import unittest
import logging
import pycurl
import socket

from twisted.internet.defer import succeed, fail

from landscape.broker.registration import (
    InvalidCredentialsError, RegistrationHandler, is_cloud_managed, EC2_API)

from landscape.broker.deployment import BrokerConfiguration
from landscape.tests.helpers import LandscapeTest, ExchangeHelper
from landscape.lib.bpickle import dumps
from landscape.lib.fetch import HTTPCodeError, FetchError


class RegistrationTest(LandscapeTest):

    helpers = [ExchangeHelper]

    def setUp(self):
        super(RegistrationTest, self).setUp()
        self.config = self.broker_service.config
        self.identity = self.broker_service.identity
        self.handler = self.broker_service.registration
        logging.getLogger().setLevel(logging.INFO)
        self.hostname = "ooga"
        self.addCleanup(setattr, socket, "gethostname", socket.gethostname)
        socket.gethostname = lambda: self.hostname

    def check_persist_property(self, attr, persist_name):
        value = "VALUE"
        self.assertEquals(getattr(self.identity, attr), None,
                          "%r attribute should default to None, not %r" %
                          (attr, getattr(self.identity, attr)))
        setattr(self.identity, attr, value)
        self.assertEquals(getattr(self.identity, attr), value,
                          "%r attribute should be %r, not %r" %
                          (attr, value, getattr(self.identity, attr)))
        self.assertEquals(self.persist.get(persist_name), value,
                          "%r not set to %r in persist" % (persist_name, value))

    def check_config_property(self, attr):
        value = "VALUE"
        setattr(self.config, attr, value)
        self.assertEquals(getattr(self.identity, attr), value,
                          "%r attribute should be %r, not %r" %
                          (attr, value, getattr(self.identity, attr)))

    def get_user_data(self, otps=None,
                      exchange_url="https://example.com/message-system",
                      ping_url="http://example.com/ping"):
        if otps is None:
            otps = ["otp1"]
        return {"otps": otps, "exchange-url": exchange_url,
                "ping-url": ping_url}

    def test_secure_id(self):
        self.check_persist_property("secure_id",
                                    "registration.secure-id")

    def test_insecure_id(self):
        self.check_persist_property("insecure_id",
                                    "registration.insecure-id")

    def test_computer_title(self):
        self.check_config_property("computer_title")

    def test_account_name(self):
        self.check_config_property("account_name")

    def test_registration_password(self):
        self.check_config_property("registration_password")

    def test_server_initiated_id_changing(self):
        """
        The server must be able to ask a client to change its secure
        and insecure ids even if no requests were sent.
        """
        self.exchanger.handle_message(
            {"type": "set-id", "id": "abc", "insecure-id": "def"})
        self.assertEquals(self.identity.secure_id, "abc")
        self.assertEquals(self.identity.insecure_id, "def")

    def test_registration_done_event(self):
        """
        When new ids are received from the server, a "registration-done"
        event is fired.
        """
        reactor_mock = self.mocker.patch(self.reactor)
        reactor_mock.fire("registration-done")
        self.mocker.replay()
        self.exchanger.handle_message(
            {"type": "set-id", "id": "abc", "insecure-id": "def"})

    def test_unknown_id(self):
        self.identity.secure_id = "old_id"
        self.identity.insecure_id = "old_id"
        self.mstore.set_accepted_types(["register"])
        self.exchanger.handle_message({"type": "unknown-id"})
        self.assertEquals(self.identity.secure_id, None)
        self.assertEquals(self.identity.insecure_id, None)

    def test_should_register(self):
        self.mstore.set_accepted_types(["register"])
        self.config.computer_title = "Computer Title"
        self.config.account_name = "account_name"
        self.assertTrue(self.handler.should_register())

    def test_should_register_with_existing_id(self):
        self.mstore.set_accepted_types(["register"])
        self.identity.secure_id = "secure"
        self.config.computer_title = "Computer Title"
        self.config.account_name = "account_name"
        self.assertFalse(self.handler.should_register())

    def test_should_register_without_computer_title(self):
        self.mstore.set_accepted_types(["register"])
        self.config.computer_title = None
        self.assertFalse(self.handler.should_register())

    def test_should_register_without_account_name(self):
        self.mstore.set_accepted_types(["register"])
        self.config.account_name = None
        self.assertFalse(self.handler.should_register())

    def test_should_register_with_unaccepted_message(self):
        self.assertFalse(self.handler.should_register())

    def test_queue_message_on_exchange(self):
        """
        When a computer_title and account_name are available, no
        secure_id is set, and an exchange is about to happen,
        queue a registration message.
        """
        self.mstore.set_accepted_types(["register"])
        self.config.computer_title = "Computer Title"
        self.config.account_name = "account_name"
        self.reactor.fire("pre-exchange")
        self.assertMessages(self.mstore.get_pending_messages(),
                            [{"type": "register",
                              "computer_title": "Computer Title",
                              "account_name": "account_name",
                              "registration_password": None,
                              "hostname": "ooga"}
                            ])
        self.assertEquals(self.logfile.getvalue().strip(),
                          "INFO: Queueing message to register with account "
                          "'account_name' without a password.")

    def test_queue_message_on_exchange_with_password(self):
        """If a registration password is available, we pass it on!"""
        self.mstore.set_accepted_types(["register"])
        self.config.computer_title = "Computer Title"
        self.config.account_name = "account_name"
        self.config.registration_password = "SEKRET"
        self.reactor.fire("pre-exchange")
        self.assertMessages(self.mstore.get_pending_messages(),
                            [{"type": "register",
                              "computer_title": "Computer Title",
                              "account_name": "account_name",
                              "registration_password": "SEKRET",
                              "hostname": "ooga"}
                            ])
        self.assertEquals(self.logfile.getvalue().strip(),
                          "INFO: Queueing message to register with account "
                          "'account_name' with a password.")

    def test_queueing_registration_message_resets_message_store(self):
        """
        When a registration message is queued, the store is reset
        entirely, since everything else that was queued is meaningless
        now that we're trying to register again.
        """
        self.mstore.set_accepted_types(["register", "test"])
        self.mstore.add({"type": "test"})
        self.config.computer_title = "Computer Title"
        self.config.account_name = "account_name"
        self.reactor.fire("pre-exchange")
        messages = self.mstore.get_pending_messages()
        self.assertEquals(len(messages), 1)
        self.assertEquals(messages[0]["type"], "register")

    def test_no_message_when_should_register_is_false(self):
        """If we already have a secure id, do not queue a register message.
        """
        handler_mock = self.mocker.patch(self.handler)
        handler_mock.should_register()
        self.mocker.result(False)

        self.mstore.set_accepted_types(["register"])
        self.config.computer_title = "Computer Title"
        self.config.account_name = "account_name"

        # If we didn't fake it, it'd work.  We do that to ensure that
        # all the needed data is in place, and that this method is
        # really what decides if a message is sent or not.  This way
        # we can test it individually.
        self.assertTrue(self.handler.should_register())

        # Now let's see.
        self.mocker.replay()

        self.reactor.fire("pre-exchange")
        self.assertMessages(self.mstore.get_pending_messages(), [])

    def test_registration_failed_event(self):
        """
        The deferred returned by a registration request should fail
        with L{InvalidCredentialsError} if the server responds with a
        failure message.
        """
        reactor_mock = self.mocker.patch(self.reactor)
        reactor_mock.fire("registration-failed")
        self.mocker.replay()
        self.exchanger.handle_message(
            {"type": "registration", "info": "unknown-account"})

    def test_registration_failed_event_not_fired_when_uncertain(self):
        """
        If the data in the registration message isn't what we expect,
        the event isn't fired.
        """
        reactor_mock = self.mocker.patch(self.reactor)
        reactor_mock.fire("registration-failed")
        self.mocker.count(0)
        self.mocker.replay()
        self.exchanger.handle_message(
            {"type": "registration", "info": "blah-blah"})

    def test_register_resets_ids(self):
        self.identity.secure_id = "foo"
        self.identity.insecure_id = "bar"
        self.handler.register()
        self.assertEquals(self.identity.secure_id, None)
        self.assertEquals(self.identity.insecure_id, None)

    def test_register_calls_urgent_exchange(self):
        exchanger_mock = self.mocker.patch(self.exchanger)
        exchanger_mock.exchange()
        self.mocker.passthrough()
        self.mocker.replay()
        self.handler.register()

    def test_register_deferred_called_on_done(self):
        # We don't want informational messages.
        self.logger.setLevel(logging.WARNING)

        calls = [0]
        d = self.handler.register()
        def add_call(result):
            self.assertEquals(result, None)
            calls[0] += 1
        d.addCallback(add_call)

        # This should somehow callback the deferred.
        self.exchanger.handle_message(
            {"type": "set-id", "id": "abc", "insecure-id": "def"})

        self.assertEquals(calls, [1])

        # Doing it again to ensure that the deferred isn't called twice.
        self.exchanger.handle_message(
            {"type": "set-id", "id": "abc", "insecure-id": "def"})

        self.assertEquals(calls, [1])

        self.assertEquals(self.logfile.getvalue(), "")

    def test_resynchronize_fired_when_registration_done(self):

        results = []
        def append():
            results.append(True)
        self.reactor.call_on("resynchronize-clients", append)

        self.handler.register()

        # This should somehow callback the deferred.
        self.exchanger.handle_message(
            {"type": "set-id", "id": "abc", "insecure-id": "def"})

        self.assertEquals(results, [True])

    def test_register_deferred_called_on_failed(self):
        # We don't want informational messages.
        self.logger.setLevel(logging.WARNING)

        calls = [0]
        d = self.handler.register()
        def add_call(failure):
            exception = failure.value
            self.assertTrue(isinstance(exception, InvalidCredentialsError))
            calls[0] += 1
        d.addErrback(add_call)

        # This should somehow callback the deferred.
        self.exchanger.handle_message(
            {"type": "registration", "info": "unknown-account"})

        self.assertEquals(calls, [1])

        # Doing it again to ensure that the deferred isn't called twice.
        self.exchanger.handle_message(
            {"type": "registration", "info": "unknown-account"})

        self.assertEquals(calls, [1])

        self.assertEquals(self.logfile.getvalue(), "")

    def test_exchange_done_calls_exchange(self):
        exchanger_mock = self.mocker.patch(self.exchanger)
        exchanger_mock.exchange()
        self.mocker.passthrough()
        self.mocker.replay()

        self.mstore.set_accepted_types(["register"])
        self.config.computer_title = "Computer Title"
        self.config.account_name = "account_name"
        self.reactor.fire("exchange-done")

    def test_exchange_done_wont_call_exchange_when_just_tried(self):
        exchanger_mock = self.mocker.patch(self.exchanger)
        exchanger_mock.exchange()
        self.mocker.count(0)
        self.mocker.replay()

        self.mstore.set_accepted_types(["register"])
        self.config.computer_title = "Computer Title"
        self.config.account_name = "account_name"
        self.reactor.fire("pre-exchange")
        self.reactor.fire("exchange-done")

    def test_default_hostname(self):
        self.mstore.set_accepted_types(["register"])
        self.config.computer_title = "Computer Title"
        self.config.account_name = "account_name"
        self.config.registration_password = "SEKRET"
        self.reactor.fire("pre-exchange")
        self.assertMessages(self.mstore.get_pending_messages(),
                            [{"type": "register",
                              "computer_title": "Computer Title",
                              "account_name": "account_name",
                              "registration_password": "SEKRET",
                              "hostname": socket.gethostname()}
                             ])

    def get_registration_handler_for_cloud(
        self, user_data=None, instance_key="key1", launch_index=0,
        local_hostname="ooga.local", public_hostname="ooga.amazon.com",
        reservation_key=u"res1", ramdisk_key=u"ram1", kernel_key=u"kernel1",
        image_key=u"image1"):
        if user_data is None:
            user_data = self.get_user_data()
        if not isinstance(user_data, Exception):
            user_data = dumps(user_data)
        api_base = "http://169.254.169.254/latest"
        query_results = {}
        for url_suffix, value in [
            ("/user-data", user_data),
            ("/meta-data/instance-id", instance_key),
            ("/meta-data/reservation-id", reservation_key),
            ("/meta-data/local-hostname", local_hostname),
            ("/meta-data/public-hostname", public_hostname),
            ("/meta-data/ami-launch-index", str(launch_index)),
            ("/meta-data/kernel-id", kernel_key),
            ("/meta-data/ramdisk-id", ramdisk_key),
            ("/meta-data/ami-id", image_key),
            ]:
            query_results[api_base + url_suffix] = value

        def fetch_stub(url):
            value = query_results[url]
            if isinstance(value, Exception):
                return fail(value)
            else:
                return succeed(value)

        exchanger = self.broker_service.exchanger
        handler = RegistrationHandler(self.broker_service.config,
                                      self.broker_service.identity,
                                      self.broker_service.reactor,
                                      exchanger,
                                      self.broker_service.pinger,
                                      self.broker_service.message_store,
                                      cloud=True,
                                      fetch_async=fetch_stub)
        return handler

    def prepare_cloud_registration(self, handler, account_name=None,
                                   registration_password=None):
        # Set things up so that the client thinks it should register
        mstore = self.broker_service.message_store
        mstore.set_accepted_types(list(mstore.get_accepted_types())
                                  + ["register-cloud-vm"])
        config = self.broker_service.config
        config.account_name = account_name
        config.registration_password = registration_password
        config.computer_title = None
        self.broker_service.identity.secure_id = None
        self.assertTrue(handler.should_register())

    def get_expected_cloud_message(self, **kwargs):
        """
        Return the message which is expected from a similar call to
        L{get_registration_handler_for_cloud}.
        """
        message = dict(type="register-cloud-vm",
                       otp="otp1",
                       hostname="ooga",
                       local_hostname="ooga.local",
                       public_hostname="ooga.amazon.com",
                       instance_key=u"key1",
                       reservation_key=u"res1",
                       ramdisk_key=u"ram1",
                       kernel_key=u"kernel1",
                       launch_index=0,
                       image_key=u"image1",
                       account_name=None,
                       registration_password=None)
        message.update(kwargs)
        return message

    def test_cloud_registration(self):
        """
        When the 'cloud' configuration variable is set, cloud registration is
        done instead of normal password-based registration. This means:

        - "Launch Data" is fetched from the EC2 Launch Data URL. This contains
          a one-time password that is used during registration.
        - A different "register-cloud-vm" message is sent to the server instead
          of "register", containing the OTP. This message is handled by
          immediately accepting the computer, instead of going through the
          pending computer stage.
        """
        handler = self.get_registration_handler_for_cloud()

        config = self.broker_service.config
        self.prepare_cloud_registration(handler)

        # metadata is fetched and stored at reactor startup:
        self.reactor.fire("run")

        # And the metadata returned determines the URLs that are used
        self.assertEquals(self.transport.get_url(),
                          "https://example.com/message-system")
        self.assertEquals(self.broker_service.pinger.get_url(),
                          "http://example.com/ping")
        # Let's make sure those values were written back to the config file
        new_config = BrokerConfiguration()
        new_config.load_configuration_file(self.config_filename)
        self.assertEquals(new_config.url, "https://example.com/message-system")
        self.assertEquals(new_config.ping_url, "http://example.com/ping")

        # Okay! Exchange should cause the registration to happen.
        self.broker_service.exchanger.exchange()
        # This *should* be asynchronous, but I think a billion tests are
        # written like this
        self.assertEquals(len(self.transport.payloads), 1)
        self.assertMessages(self.transport.payloads[0]["messages"],
                            [self.get_expected_cloud_message()])

    def test_wrong_user_data(self):
        handler = self.get_registration_handler_for_cloud(
            user_data="other stuff, not a bpickle")
        config = self.broker_service.config

        exchanger = self.broker_service.exchanger

        self.prepare_cloud_registration(handler)

        # Mock registration-failed call
        reactor_mock = self.mocker.patch(self.reactor)
        reactor_mock.fire("registration-failed")
        self.mocker.replay()

        self.reactor.fire("run")
        exchanger.exchange()

    def test_wrong_object_type_in_user_data(self):
        handler = self.get_registration_handler_for_cloud(
            user_data=True)
        config = self.broker_service.config

        exchanger = self.broker_service.exchanger

        self.prepare_cloud_registration(handler)

        # Mock registration-failed call
        reactor_mock = self.mocker.patch(self.reactor)
        reactor_mock.fire("registration-failed")
        self.mocker.replay()

        self.reactor.fire("run")
        exchanger.exchange()

    def test_user_data_with_not_enough_elements(self):
        """
        If the AMI launch index isn't represented in the list of OTPs in the
        user data then BOOM.
        """
        handler = self.get_registration_handler_for_cloud(launch_index=1)

        self.prepare_cloud_registration(handler)

        # Mock registration-failed call
        reactor_mock = self.mocker.patch(self.reactor)
        reactor_mock.fire("registration-failed")
        self.mocker.replay()

        self.reactor.fire("run")
        self.broker_service.exchanger.exchange()

    def test_user_data_bpickle_without_otp(self):
        handler = self.get_registration_handler_for_cloud(
            user_data={"foo": "bar"})
        self.prepare_cloud_registration(handler)

        # Mock registration-failed call
        reactor_mock = self.mocker.patch(self.reactor)
        reactor_mock.fire("registration-failed")
        self.mocker.replay()

        self.reactor.fire("run")
        self.broker_service.exchanger.exchange()

    def test_no_otp_fallback_to_account(self):
        handler = self.get_registration_handler_for_cloud(
            user_data="other stuff, not a bpickle",
            instance_key=u"key1")
        self.prepare_cloud_registration(handler,
                                        account_name=u"onward",
                                        registration_password=u"password")

        self.reactor.fire("run")
        self.broker_service.exchanger.exchange()

        self.assertEquals(len(self.transport.payloads), 1)
        self.assertMessages(self.transport.payloads[0]["messages"],
                            [self.get_expected_cloud_message(
                                otp=None,
                                account_name=u"onward",
                                registration_password=u"password")])

    def test_queueing_cloud_registration_message_resets_message_store(self):
        """
        When a registration from a cloud is about to happen, the message store
        is reset, because all previous messages are now meaningless.
        """
        self.mstore.set_accepted_types(list(self.mstore.get_accepted_types())
                                       + ["test"])

        self.mstore.add({"type": "test"})

        handler = self.get_registration_handler_for_cloud()

        self.prepare_cloud_registration(handler)

        self.reactor.fire("run")
        self.reactor.fire("pre-exchange")

        messages = self.mstore.get_pending_messages()
        self.assertEquals(len(messages), 1)
        self.assertEquals(messages[0]["type"], "register-cloud-vm")

    def test_cloud_registration_fetch_errors(self):
        """
        If fetching metadata fails, and we have no account details to fall
        back to, we fire 'registration-failed'.
        """
        self.log_helper.ignore_errors(pycurl.error)
        config = self.broker_service.config

        def fetch_stub(url):
            return fail(pycurl.error(7, "couldn't connect to host"))

        exchanger = self.broker_service.exchanger
        handler = RegistrationHandler(self.broker_service.config,
                                      self.broker_service.identity,
                                      self.broker_service.reactor,
                                      exchanger,
                                      self.broker_service.pinger,
                                      self.broker_service.message_store,
                                      cloud=True,
                                      fetch_async=fetch_stub)

        self.prepare_cloud_registration(handler)

        failed = []
        self.reactor.call_on("registration-failed", lambda: failed.append(True))

        self.log_helper.ignore_errors("Got error while fetching meta-data")
        self.reactor.fire("run")
        exchanger.exchange()
        self.assertEquals(failed, [True])
        self.assertIn('error: (7, "couldn\'t connect to host")',
                      self.logfile.getvalue())

    def test_cloud_registration_continues_without_user_data(self):
        """
        If no user-data exists (i.e., the user-data URL returns a 404), then
        register-cloud-vm still occurs.
        """
        self.log_helper.ignore_errors(HTTPCodeError)
        handler = self.get_registration_handler_for_cloud(
            user_data=HTTPCodeError(404, "ohno"))
        self.prepare_cloud_registration(handler,
                                        account_name="onward",
                                        registration_password="password")

        self.reactor.fire("run")
        self.broker_service.exchanger.exchange()
        self.assertIn("HTTPCodeError: Server returned HTTP code 404",
                      self.logfile.getvalue())
        self.assertEquals(len(self.transport.payloads), 1)
        self.assertMessages(self.transport.payloads[0]["messages"],
                            [self.get_expected_cloud_message(
                                otp=None,
                                account_name=u"onward",
                                registration_password=u"password")])

    def test_fall_back_to_normal_registration_when_metadata_fetch_fails(self):
        """
        If fetching metadata fails, but we do have an account name, then we
        fall back to normal 'register' registration.
        """
        self.mstore.set_accepted_types(["register"])
        self.log_helper.ignore_errors(HTTPCodeError)
        handler = self.get_registration_handler_for_cloud(
            public_hostname=HTTPCodeError(404, "ohnoes"))
        self.prepare_cloud_registration(handler,
                                        account_name="onward",
                                        registration_password="password")
        self.broker_service.config.computer_title = "whatever"
        self.reactor.fire("run")
        self.broker_service.exchanger.exchange()
        self.assertIn("HTTPCodeError: Server returned HTTP code 404",
                      self.logfile.getvalue())
        self.assertEquals(len(self.transport.payloads), 1)
        self.assertMessages(self.transport.payloads[0]["messages"],
                            [{"type": "register",
                              "computer_title": u"whatever",
                              "account_name": u"onward",
                              "registration_password": u"password",
                              "hostname": socket.gethostname()}])

    def test_should_register_in_cloud(self):
        """
        The client should register when it's in the cloud even though
        it doesn't have the normal account details.
        """
        config = self.broker_service.config
        handler = RegistrationHandler(self.broker_service.config,
                                      self.broker_service.identity,
                                      self.broker_service.reactor,
                                      self.broker_service.exchanger,
                                      self.broker_service.pinger,
                                      self.broker_service.message_store,
                                      cloud=True)

        mstore = self.broker_service.message_store
        mstore.set_accepted_types(mstore.get_accepted_types()
                                  + ("register-cloud-vm",))
        config.account_name = None
        config.registration_password = None
        config.computer_title = None
        self.broker_service.identity.secure_id = None
        self.assertTrue(handler.should_register())

    def test_launch_index(self):
        """
        The client used the value in C{ami-launch-index} to choose the
        appropriate OTP in the user data.
        """
        otp = "correct otp for launch index"
        handler = self.get_registration_handler_for_cloud(
            user_data=self.get_user_data(otps=["wrong index",
                                               otp,
                                               "wrong again"],),
            instance_key="key1",
            launch_index=1)

        self.prepare_cloud_registration(handler)

        self.reactor.fire("run")
        self.broker_service.exchanger.exchange()
        self.assertEquals(len(self.transport.payloads), 1)
        self.assertMessages(self.transport.payloads[0]["messages"],
                            [self.get_expected_cloud_message(otp=otp,
                                                             launch_index=1)])

    def test_should_not_register_in_cloud(self):
        """
        Having a secure ID means we shouldn't register, even in the cloud.
        """
        config = self.broker_service.config
        handler = RegistrationHandler(self.broker_service.config,
                                      self.broker_service.identity,
                                      self.broker_service.reactor,
                                      self.broker_service.exchanger,
                                      self.broker_service.pinger,
                                      self.broker_service.message_store,
                                      cloud=True)

        mstore = self.broker_service.message_store
        mstore.set_accepted_types(mstore.get_accepted_types()
                                  + ("register-cloud-vm",))
        config.account_name = None
        config.registration_password = None
        config.computer_title = None
        self.broker_service.identity.secure_id = "hello"
        self.assertFalse(handler.should_register())

    def test_should_not_register_without_register_cloud_vm(self):
        """
        If the server isn't accepting a 'register-cloud-vm' message,
        we shouldn't register.
        """
        config = self.broker_service.config
        handler = RegistrationHandler(self.broker_service.config,
                                      self.broker_service.identity,
                                      self.broker_service.reactor,
                                      self.broker_service.exchanger,
                                      self.broker_service.pinger,
                                      self.broker_service.message_store,
                                      cloud=True)

        config.account_name = None
        config.registration_password = None
        config.computer_title = None
        self.broker_service.identity.secure_id = None
        self.assertFalse(handler.should_register())


class IsCloudManagedTests(unittest.TestCase):

    def setUp(self):
        self.urls = []
        self.responses = []

    def fake_fetch(self, url):
        self.urls.append(url)
        return self.responses.pop(0)

    def test_is_managed(self):
        user_data = {"otps": ["otp1"], "exchange-url": "http://exchange",
                     "ping-url": "http://ping"}
        self.responses = [dumps(user_data), "0"]
        self.assertTrue(is_cloud_managed(self.fake_fetch))
        self.assertEquals(
            self.urls,
            [EC2_API + "/user-data", EC2_API + "/meta-data/ami-launch-index"])

    def test_is_managed_index(self):
        user_data = {"otps": ["otp1", "otp2"], "exchange-url": "http://exchange",
                     "ping-url": "http://ping"}
        self.responses = [dumps(user_data), "1"]
        self.assertTrue(is_cloud_managed(self.fake_fetch))

    def test_is_managed_wrong_index(self):
        user_data = {"otps": ["otp1"], "exchange-url": "http://exchange",
                     "ping-url": "http://ping"}
        self.responses = [dumps(user_data), "1"]
        self.assertFalse(is_cloud_managed(self.fake_fetch))

    def test_is_managed_exchange_url(self):
        user_data = {"otps": ["otp1"], "ping-url": "http://ping"}
        self.responses = [dumps(user_data), "0"]
        self.assertFalse(is_cloud_managed(self.fake_fetch))

    def test_is_managed_ping_url(self):
        user_data = {"otps": ["otp1"], "exchange-url": "http://exchange"}
        self.responses = [dumps(user_data), "0"]
        self.assertFalse(is_cloud_managed(self.fake_fetch))

    def test_is_managed_bpickle(self):
        self.responses = ["some other user data", "0"]
        self.assertFalse(is_cloud_managed(self.fake_fetch))

    def test_is_managed_no_data(self):
        self.responses = ["", "0"]
        self.assertFalse(is_cloud_managed(self.fake_fetch))

    def test_is_managed_fetch_not_found(self):
        def fake_fetch(url):
            raise HTTPCodeError(404, "ohnoes")
        self.assertFalse(is_cloud_managed(fake_fetch))

    def test_is_managed_fetch_error(self):
        def fake_fetch(url):
            raise FetchError(7, "couldn't connect to host")
        self.assertFalse(is_cloud_managed(fake_fetch))
