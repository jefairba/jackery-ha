"""API client for Jackery cloud services."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import secrets
import threading
import time
import uuid
from typing import Optional

import requests
from Cryptodome.Cipher import AES, PKCS1_v1_5
from Cryptodome.PublicKey import RSA
from Cryptodome.Util.Padding import pad

try:
    import aiomqtt
except ModuleNotFoundError:
    aiomqtt = None

try:
    import socketry
    from socketry.client import _mqtt_params as _socketry_mqtt_params
except ModuleNotFoundError as err:  # pragma: no cover - dependency provided in prod
    if err.name == "socketry":
        socketry = None
        _socketry_mqtt_params = None
    else:
        raise

_LOGGER = logging.getLogger(__name__)

# Keys whose values must never reach the log: credentials, session secrets,
# and identifiers that locate the user or their network.
_REDACT_KEYS = frozenset(
    {
        "token",
        "mqttPassWord",
        "password",
        "account",
        "userId",
        "bindUserId",
        "macId",
        "wname",
        "wip",
        "mac",
        # device and battery-pack serial numbers
        "deviceSn",
        "deviceCode",
        "devSn",
        "sn",
    }
)


def _redact(value):
    """Return a copy of an API payload with sensitive values masked."""
    if isinstance(value, dict):
        return {
            k: "**REDACTED**" if k in _REDACT_KEYS else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _describe_request_error(err: requests.RequestException) -> str:
    """Describe a request failure without echoing the request URL.

    requests puts the full URL (including query parameters) in its error
    messages. For login that URL carries ``aesEncryptData``, which is the
    password encrypted with a public, hardcoded key - i.e. recoverable.
    """
    response = getattr(err, "response", None)
    if response is not None:
        return f"{type(err).__name__} (HTTP {response.status_code})"
    return type(err).__name__


class JackeryMqttSession:
    """Persistent single-connection MQTT session for Jackery API.

    Owns one long-lived ``aiomqtt.Client`` and routes incoming messages to the
    active pending-request future (queries / commands) or drops them as push
    data.  A single ``_operation_lock`` serialises publish+wait operations so
    response matching is unambiguous - only one in-flight MQTT request exists
    at a time.
    """

    _MAX_RECONNECT_DELAY = 30.0

    def __init__(self, api: "JackeryAPI") -> None:
        self._api = api
        self._client: "aiomqtt.Client | None" = None
        self._user_id: "str | None" = None
        self._loop_task: "asyncio.Task | None" = None
        self._running = False
        self._reconnect_delay = 1.0
        self._operation_lock = asyncio.Lock()
        self._pending_matcher: "callable | None" = None
        self._pending_future: "asyncio.Future | None" = None
        self._connected = asyncio.Event()
        self._push_handlers: dict[str, callable] = {}

    async def start(self) -> None:
        """Start the background reconnect loop."""
        self._running = True
        self._loop_task = asyncio.create_task(
            self._run_loop(), name="jackery_mqtt_session"
        )

    async def stop(self) -> None:
        """Cancel the background loop and clean up."""
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None
        self._fail_pending(RuntimeError("MQTT session stopped"))

    async def _wait_out_yield(self) -> None:
        """Sleep while HA is yielding the account to the phone app.

        The MQTT client ID is ``{userId}@APP`` - the same one the phone app
        uses - so merely reconnecting would kick the app off. Checks every few
        seconds so 'Reclaim Jackery Session' takes effect promptly.
        """
        while self._running and self._api.is_yielding():
            await asyncio.sleep(min(5.0, max(0.5, self._api.yield_remaining())))

    async def _run_loop(self) -> None:
        """Reconnect loop - runs until stop() is called."""
        check_session_first = False
        while self._running:
            try:
                await self._wait_out_yield()
                if not self._running:
                    return
                if check_session_first:
                    # A dropped connection often means the phone app just
                    # logged in. Ask the HTTP API (cheap) before reconnecting;
                    # on 10403 this starts a yield instead of a tug-of-war.
                    try:
                        await asyncio.to_thread(self._api.check_session)
                    except JackerySessionYielded:
                        continue
                    except Exception as err:  # noqa: BLE001 - network trouble
                        _LOGGER.debug("Session check before reconnect failed: %s", err)
                params, user_id = self._api._build_mqtt_params()
                dev_topic = f"hb/app/{user_id}/device"
                async with aiomqtt.Client(**params) as client:
                    self._client = client
                    self._user_id = user_id
                    self._reconnect_delay = 1.0
                    check_session_first = True
                    await client.subscribe(dev_topic, qos=1)
                    self._connected.set()
                    _LOGGER.info("Jackery MQTT persistent session connected")
                    async for message in client.messages:
                        self._dispatch(message)
            except asyncio.CancelledError:
                return
            except Exception as err:
                _LOGGER.warning(
                    "Jackery MQTT session error: %s; reconnecting in %.0fs",
                    err,
                    self._reconnect_delay,
                )
            finally:
                self._client = None
                self._connected.clear()
                self._fail_pending(RuntimeError("MQTT session disconnected"))

            if not self._running:
                return
            try:
                await asyncio.sleep(self._reconnect_delay)
            except asyncio.CancelledError:
                return
            self._reconnect_delay = min(
                self._reconnect_delay * 2, self._MAX_RECONNECT_DELAY
            )

    def register_push_handler(self, device_sn: str, handler: callable) -> None:
        """Register a handler for unsolicited push messages from device_sn."""
        self._push_handlers[device_sn] = handler

    def _dispatch(self, message) -> None:
        """Route an incoming message to a pending future or a push handler."""
        try:
            data = json.loads(message.payload)
        except (json.JSONDecodeError, TypeError):
            return

        if (
            self._pending_future is not None
            and not self._pending_future.done()
            and self._pending_matcher is not None
            and self._pending_matcher(data)
        ):
            self._pending_future.set_result(data)
            return

        device_sn = data.get("deviceSn", "")
        handler = self._push_handlers.get(device_sn)
        if handler is not None:
            handler(data)
        else:
            _LOGGER.debug(
                "MQTT push: deviceSn=%s actionId=%s",
                device_sn,
                data.get("actionId"),
            )

    def _fail_pending(self, err: Exception) -> None:
        """Fail the pending future (no-op if none outstanding)."""
        if self._pending_future is not None and not self._pending_future.done():
            self._pending_future.set_exception(err)
        self._pending_matcher = None
        self._pending_future = None

    async def publish_and_wait(
        self,
        payload: dict,
        matcher: "callable",
        timeout: float = 10.0,
    ) -> dict:
        """Publish a command and await a matching response.

        Waits up to *timeout* seconds total: first for the session to connect
        (handles startup latency), then for a matching response after publish.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        if not self._connected.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("MQTT session not connected")
            try:
                async with asyncio.timeout(remaining):
                    await self._connected.wait()
            except TimeoutError:
                raise TimeoutError(
                    f"MQTT session did not connect within {timeout:.0f}s"
                ) from None

        async with self._operation_lock:
            if self._client is None or self._user_id is None:
                raise RuntimeError("MQTT session disconnected")

            loop = asyncio.get_running_loop()
            self._pending_future = loop.create_future()
            self._pending_matcher = matcher
            try:
                cmd_topic = f"hb/app/{self._user_id}/command"
                payload_str = json.dumps(payload, separators=(",", ":"))
                await self._client.publish(cmd_topic, payload_str, qos=1)

                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("MQTT operation timed out after publish")
                async with asyncio.timeout(remaining):
                    return await self._pending_future
            finally:
                self._pending_matcher = None
                self._pending_future = None


# How long to wait for the device to echo a command back through Jackery's
# cloud. Long enough for a slow round trip, short enough that someone at the
# dashboard is still watching when the answer (or the error) arrives.
COMMAND_CONFIRM_TIMEOUT_SEC = 10.0

# When a device only acknowledges a command, read its state back to see if
# the command actually took effect. Jackery's cloud copy lags the device by a
# second or two, so check a few times (~6 s total) before calling it refused.
READBACK_DELAYS_SEC = (1.5, 2.0, 2.5)

# Body keys that are opcodes/addresses rather than commanded values.
_NON_VALUE_KEYS = frozenset({"cmd", "idx"})


# Default time to stay signed out after the phone app takes the session.
DEFAULT_YIELD_SECONDS = 15 * 60


class JackeryCommandError(Exception):
    """A device command did not complete."""


class JackerySessionYielded(JackeryCommandError):
    """HA is deliberately signed out so the phone app can use the account."""


class JackeryCommandUnconfirmed(JackeryCommandError):
    """No confirmation arrived in time; the command may or may not have applied."""


class JackeryCommandRejected(JackeryCommandError):
    """The device confirmed, but reports a value other than the one sent."""


def _same_value(sent, reported) -> bool:
    try:
        return int(sent) == int(reported)
    except (TypeError, ValueError):
        return str(sent) == str(reported)


def _commanded_values(sent_body: dict) -> dict:
    return {k: v for k, v in sent_body.items() if k not in _NON_VALUE_KEYS}


def _reply_reports_values(sent_body: dict, reply) -> bool:
    """True if the reply echoes at least one commanded value (portables do).

    The Smart Transfer Switch only acknowledges - e.g. {"cmd": 5,
    "messageId": ...} for Force Charge (seen 2026-09-26) - so its commands are
    confirmed by reading the device state back instead.
    """
    expected = _commanded_values(sent_body)
    return isinstance(reply, dict) and bool(expected.keys() & reply.keys())


def _verify_reply(sent_body: dict, reply) -> None:
    """Check that a reply that echoes values reports the ones we commanded."""
    expected = _commanded_values(sent_body)
    mismatched = {
        k: (v, reply[k])
        for k, v in expected.items()
        if k in reply and not _same_value(v, reply[k])
    }
    if mismatched:
        detail = ", ".join(
            f"{k}: sent {sent}, device reports {got}"
            for k, (sent, got) in mismatched.items()
        )
        raise JackeryCommandRejected(f"device kept its previous value ({detail})")


def new_android_id() -> str:
    """Return a fresh random device ID in Android ID format (16 hex chars)."""
    return secrets.token_hex(8)


class JackeryAuthenticationError(Exception):
    """Jackery rejected the credentials (reauth needed)."""


class JackeryConnectionError(Exception):
    """Jackery could not be reached; transient, never a reason to reauth.

    Kept separate from JackeryAuthenticationError so an internet outage
    (e.g. HA restarting after a power cut before the router is back) is
    retried instead of parking the entry in "reauthentication required".
    """


class JackeryAPI:
    """A client to interact with the Jackery Cloud API."""

    def __init__(
        self, account: str, password: str, android_id: str | None = None
    ):
        """Initialize the API client.

        ``android_id`` is the device identity presented to Jackery (it seeds
        the login macId and the MQTT username). Callers should pass a stable
        per-install value; without one a random ID is used for this instance.
        """
        self.account = account
        self.password = password
        self.android_id = android_id or new_android_id()
        self.base_url = "https://iot.jackeryapp.com"
        self._token: Optional[str] = None
        self._token_expiry_time: float = (
            0  # We will assume a long expiry for simplicity
        )
        self._mqtt_user_id: Optional[str] = None
        self._mqtt_password_b64: Optional[str] = None
        self._mac_id: str = self._generate_udid()
        self._control_client = None
        self._control_client_lock = asyncio.Lock()
        self._control_write_lock = asyncio.Lock()
        self._login_lock = threading.Lock()
        self._last_login_time: float = 0
        self._mqtt_session: Optional[JackeryMqttSession] = None
        # Jackery allows one login per account. When another client (the
        # phone app) takes the session, back off for this long instead of
        # logging straight back in and kicking it out. 0 = never yield.
        self.yield_seconds: float = DEFAULT_YIELD_SECONDS
        self._yield_until: Optional[float] = None
        self._yield_lock = threading.Lock()

    def _name_uuid_from_bytes_java(self, data: bytes) -> str:
        """Generate a version 3 UUID using an MD5 hash."""
        md5_digest = hashlib.md5(data).digest()
        u = uuid.UUID(bytes=md5_digest, version=3)
        return str(u).replace("-", "")

    def _generate_udid(self) -> str:
        """Generate a UDID."""
        if self.android_id and self.android_id != "9774d56d682e549c":
            return "2" + self._name_uuid_from_bytes_java(
                self.android_id.encode("utf-8")
            )
        else:
            random_uuid_str = str(uuid.uuid4()).replace("-", "")
            return "9" + random_uuid_str

    def _encrypt_with_aes(self, plain_text: str, aes_key: bytes) -> str:
        """Perform AES encryption."""
        cipher = AES.new(aes_key, AES.MODE_ECB)
        encrypted = cipher.encrypt(pad(plain_text.encode("utf-8"), AES.block_size))
        return base64.b64encode(encrypted).decode("utf-8")

    def _encrypt_with_rsa(self, data: bytes, public_key_b64: str) -> str:
        """Perform RSA encryption."""
        pub_key_pem = (
            f"-----BEGIN PUBLIC KEY-----\n{public_key_b64}\n-----END PUBLIC KEY-----"
        )
        pub_key = RSA.importKey(pub_key_pem)
        cipher = PKCS1_v1_5.new(pub_key)
        encrypted = cipher.encrypt(data)
        return base64.b64encode(encrypted).decode("utf-8")

    def login(self) -> bool:
        """Perform the login process and store the token."""
        with self._login_lock:
            # If another thread just logged in, reuse its credentials
            if time.monotonic() - self._last_login_time < 5 and self._token:
                _LOGGER.debug("Skipping login - another thread refreshed credentials %.1fs ago",
                              time.monotonic() - self._last_login_time)
                return True
            return self._login_inner()

    def _login_inner(self) -> bool:
        """Actual login implementation (must be called with _login_lock held)."""
        _LOGGER.info("Attempting to login to Jackery service")
        mac_id = self._generate_udid()
        login_bean = {
            "account": self.account,
            "loginType": 2,
            "macId": mac_id,
            "password": self.password,
            "phone": "",
            "registerAppId": "com.hbxn.jackery",
            "verificationCode": "",
        }

        public_key_b64 = "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCVmzgJy/4XolxPnkfu32YtJqYGFLYqf9/rnVgURJED+8J9J3Pccd6+9L97/+7COZE5OkejsgOkqeLNC9C3r5mhpE4zk/HStss7Q8/5DqkGD1annQ+eoICo3oi0dITZ0Qll56Dowb8lXi6WHViVDdih/oeUwVJY89uJNtTWrz7t7QIDAQAB"
        aes_key = b"1234567890123456"
        login_bean_json = json.dumps(login_bean, ensure_ascii=False)
        aes_encrypt_data = self._encrypt_with_aes(login_bean_json, aes_key)
        rsa_for_aes_key = self._encrypt_with_rsa(aes_key, public_key_b64)

        url = f"{self.base_url}/v1/auth/login"
        params = {"aesEncryptData": aes_encrypt_data, "rsaForAesKey": rsa_for_aes_key}
        headers = {
            "app_version": "1.0.5",
            "upload-incomplete": "?0",
            "sys_version": "17.2",
            "platform": "1",
            "upload-draft-interop-version": "3",
            "accept": "*/*",
            "accept-language": "en-US",
            "accept-encoding": "br;q=1.0, gzip;q=0.9, deflate;q=0.8",
            "User-Agent": "DxPowerProject/1.0.5 (com.hb.jackery; build:2; iOS 17.2.0) Alamofire/5.8.0",
            "model": "iPad Pro (12.9-inch) (3rd generation)",
        }
        files = {"file": ("", b"", "")}

        try:
            response = requests.post(
                url, params=params, headers=headers, files=files, timeout=10
            )
            _LOGGER.debug("Login response status: %s", response.status_code)
            response.raise_for_status()
            data = response.json()
            _LOGGER.debug("Login response data: %s", _redact(data))

            if data.get("code") == 0 and "token" in data:
                self._token = data["token"]
                login_data = data.get("data", {})
                self._mqtt_user_id = str(login_data.get("userId", ""))
                self._mqtt_password_b64 = str(login_data.get("mqttPassWord", ""))
                self._last_login_time = time.monotonic()
                # Invalidate cached socketry client so next control use rebuilds
                # with fresh credentials rather than a stale token.
                self._control_client = None
                _LOGGER.info("Successfully logged in and obtained token.")
                return True
            else:
                error_msg = f"Login failed: {data.get('msg', 'Unknown error')} (code: {data.get('code')})"
                _LOGGER.error(error_msg)
                raise JackeryAuthenticationError(data.get("msg", "Login failed"))
        except requests.RequestException as e:
            reason = _describe_request_error(e)
            _LOGGER.error("Login request failed: %s", reason)
            # "from None": the chained exception's message contains the URL.
            raise JackeryConnectionError(f"Request failed: {reason}") from None

    # ---- Session yielding -------------------------------------------------

    def is_yielding(self) -> bool:
        """True while HA is deliberately leaving the session to another client."""
        with self._yield_lock:
            if self._yield_until is None:
                return False
            if time.monotonic() >= self._yield_until:
                self._yield_until = None
                return False
            return True

    def yield_remaining(self) -> float:
        """Seconds left in the current yield (0 when not yielding)."""
        with self._yield_lock:
            if self._yield_until is None:
                return 0.0
            return max(0.0, self._yield_until - time.monotonic())

    def reclaim_session(self) -> None:
        """End a yield early; the next request logs in again."""
        with self._yield_lock:
            self._yield_until = None
        _LOGGER.info("Reclaiming Jackery session (yield ended by user)")

    def _start_yield(self) -> None:
        with self._yield_lock:
            self._yield_until = time.monotonic() + self.yield_seconds
            self._token = None
        _LOGGER.warning(
            "Jackery session taken by another login (likely the phone app); "
            "HA will stay signed out for %.0f min. Use 'Reclaim Jackery Session' "
            "to take it back sooner.",
            self.yield_seconds / 60,
        )

    def _raise_if_yielding(self) -> None:
        if self.is_yielding():
            raise JackerySessionYielded(
                "HA is leaving the Jackery session to the phone app for another "
                f"{self.yield_remaining() / 60:.0f} min; press 'Reclaim Jackery "
                "Session' to take it back now"
            )

    # ---- HTTP ----------------------------------------------------------------

    def _request(
        self,
        method: str,
        url_path: str,
        params: Optional[dict] = None,
        form: Optional[dict] = None,
    ) -> dict:
        """Make an authenticated API request, handling token expiry.

        On 10403 (session displaced by another login) HA yields for
        ``yield_seconds`` instead of immediately logging back in, which would
        just kick the phone app out again.
        """
        self._raise_if_yielding()
        if not self._token:
            _LOGGER.info("No token found, logging in.")
            if not self.login():
                raise JackeryAuthenticationError("Unable to login to retrieve token.")

        headers = {
            "accept": "*/*",
            "app_version": "1.0.5",
            "sys_version": "17.2",
            "accept-encoding": "br;q=1.0, gzip;q=0.9, deflate;q=0.8",
            "accept-language": "en-US",
            "platform": "1",
            "user-agent": "DxPowerProject/1.0.5 (com.hb.jackery; build:2; iOS 17.2.0) Alamofire/5.8.0",
            "model": "iPad Pro (12.9-inch) (3rd generation)",
            "token": self._token,
        }
        if form is None:
            headers["content-type"] = "application/json"
        full_url = f"{self.base_url}{url_path}"
        _LOGGER.debug("Making API %s request to: %s", method, full_url)

        def send() -> dict:
            if method == "POST":
                response = requests.post(
                    full_url, headers=headers, params=params, data=form, timeout=10
                )
            else:
                response = requests.get(
                    full_url, headers=headers, params=params, timeout=10
                )
            _LOGGER.debug("API response status: %s", response.status_code)
            response.raise_for_status()
            return response.json()

        try:
            data = send()
            _LOGGER.debug("API response data: %s", _redact(data))

            code = data.get("code")
            if code == 10403 and self.yield_seconds > 0:
                self._start_yield()
                self._raise_if_yielding()
            # 10402 = token expired; 10403 with yielding disabled = take it back
            if code in (10402, 10403):
                _LOGGER.info("Re-logging in (code=%s)...", code)
                if not self.login():
                    raise JackeryAuthenticationError(
                        "Failed to re-login after session invalidated."
                    )
                headers["token"] = self._token
                data = send()

            if data.get("code") != 0:
                error_msg = f"API Error: {data.get('msg', 'Unknown error')} (code: {data.get('code')})"
                _LOGGER.error(error_msg)
                raise Exception(error_msg)

            return data

        except requests.RequestException as e:
            _LOGGER.error("API request failed: %s", _describe_request_error(e))
            raise

    def _get_request(self, url_path: str, params: Optional[dict] = None) -> dict:
        """Make a GET request to the API, handling token expiry."""
        return self._request("GET", url_path, params=params)

    def check_session(self) -> None:
        """Cheap authenticated call; raises JackerySessionYielded if displaced."""
        self._get_request("/v1/device/bind/shared")

    # ---- Devices -------------------------------------------------------------

    def get_device_list(self) -> dict:
        """Get owned devices plus devices other accounts have shared with us."""
        _LOGGER.info("Attempting to fetch device list from Jackery API")
        try:
            result = self._get_request("/v1/device/bind/list")
            _LOGGER.info("Successfully retrieved device list")
        except Exception as e:
            _LOGGER.error("Failed to get device list: %s", str(e))
            raise

        owned = list(result.get("data") or [])
        try:
            shared = self._get_shared_devices({d.get("devSn") for d in owned})
        except JackerySessionYielded:
            raise
        except Exception as e:  # noqa: BLE001 - shared devices are optional
            _LOGGER.warning("Could not fetch devices shared with this account: %s", e)
            shared = []
        if shared:
            _LOGGER.info("Found %d device(s) shared with this account", len(shared))
        return {**result, "data": owned + shared}

    def _get_shared_devices(self, known_sns: set) -> list[dict]:
        """Devices shared *to* this account by other accounts.

        Mirrors socketry: /device/bind/shared lists the accounts sharing with
        us ("receive"); /device/bind/share/list returns each one's devices.
        What a shared account may do (read vs control) is decided by Jackery
        and not known here; ``shareLevel`` is kept for diagnosis.
        """
        shared_data = self._get_request("/v1/device/bind/shared").get("data") or {}
        devices: list[dict] = []
        for share in shared_data.get("receive") or []:
            body = self._request(
                "POST",
                "/v1/device/bind/share/list",
                form={
                    "bindUserId": str(share.get("bindUserId", "")),
                    "level": str(share.get("level", "")),
                },
            )
            for device in body.get("data") or []:
                sn = device.get("devSn")
                if not sn or sn in known_sns:
                    continue
                known_sns.add(sn)
                devices.append(
                    {
                        **device,
                        "devName": device.get("devNickname")
                        or device.get("devName")
                        or sn,
                        "shared": True,
                        "shareLevel": share.get("level"),
                    }
                )
        return devices

    def get_device_detail(self, device_id: str) -> dict:
        """Get detailed information for a specified device."""
        return self._get_request("/v1/device/property", params={"deviceId": device_id})

    async def _async_get_control_client(self):
        """Get an authenticated Socketry client for device control."""
        async with self._control_client_lock:
            if self._control_client is not None:
                return self._control_client

            if socketry is None:
                raise RuntimeError("socketry is not installed")
            if not self._mqtt_user_id or not self._mqtt_password_b64 or not self._token:
                raise RuntimeError("MQTT credentials not available - call login() first")

            # Build from existing HA credentials to avoid a new HTTP login that
            # would rotate the auth token. Omitting email/password prevents
            # socketry from ever calling _http_login internally.
            creds = {
                "userId": self._mqtt_user_id,
                "mqttPassWord": self._mqtt_password_b64,
                "token": self._token,
                "macId": self._mac_id,
                "deviceSn": "",
                "deviceId": "",
                "deviceName": "",
                "devices": [],
            }
            client = None
            try:
                client = socketry.Client(creds)
                await client.fetch_devices()
            except asyncio.CancelledError:
                await self._async_close_control_client(client)
                raise
            except Exception:
                await self._async_close_control_client(client)
                raise
            self._control_client = client
            return client

    async def start_mqtt_session(self) -> None:
        """Start the persistent MQTT session (no-op if dependencies are missing)."""
        if aiomqtt is None or _socketry_mqtt_params is None:
            return
        if self._mqtt_session is not None:
            return
        self._mqtt_session = JackeryMqttSession(self)
        await self._mqtt_session.start()

    async def stop_mqtt_session(self) -> None:
        """Stop the persistent MQTT session."""
        if self._mqtt_session is not None:
            await self._mqtt_session.stop()
            self._mqtt_session = None

    def register_push_handler(self, device_sn: str, handler: callable) -> None:
        """Register a handler for unsolicited MQTT push messages from device_sn."""
        if self._mqtt_session is not None:
            self._mqtt_session.register_push_handler(device_sn, handler)

    async def async_close(self) -> None:
        """Stop the MQTT session and release the cached Socketry control client."""
        await self.stop_mqtt_session()
        async with self._control_write_lock:
            await self._async_reset_control_client()

    async def _async_reset_control_client(self, client=None) -> None:
        """Drop the cached control client and close the discarded instance."""
        async with self._control_client_lock:
            client_to_close = self._control_client if client is None else client
            if client is None:
                self._control_client = None
            elif self._control_client is client:
                self._control_client = None

        await self._async_close_control_client(client_to_close)

    async def _async_close_control_client(self, client) -> None:
        """Best-effort close a discarded Socketry control client."""
        if client is None:
            return

        for method_name in ("stop", "disconnect", "close"):
            method = getattr(client, method_name, None)
            if method is None:
                continue

            try:
                result = method()
                if inspect.isawaitable(result):
                    await result
                break  # first successful close wins; don't try remaining methods
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pragma: no cover - defensive cleanup
                _LOGGER.debug(
                    "Failed to %s discarded Socketry control client: %s",
                    method_name,
                    err,
                )

    async def async_set_device_property(
        self,
        device_id: str,
        device_sn: str,
        property_slug: str,
        value: str | int,
    ) -> None:
        """Set a portable device property via the persistent MQTT session.

        Routes all writes through JackeryMqttSession so that controls and the
        push/poll subscription share one MQTT connection (client ID
        ``{userId}@APP``).  Using a separate socketry control connection with the
        same client ID would kick out the persistent session, causing the Jackery
        backend to treat it as a competing app login and invalidate the REST
        token (error 10403).
        """
        if aiomqtt is None:
            raise RuntimeError("aiomqtt is not installed")
        if self._mqtt_session is None:
            raise RuntimeError("MQTT session not started - call start_mqtt_session() first")

        # Look up action_id and prop_key from our own control spec registry first
        # (avoids depending on socketry's internal property definitions at write time).
        from .protocol import CONTROL_SPECS_BY_SLUG
        spec = CONTROL_SPECS_BY_SLUG.get(property_slug)
        if spec is not None and spec.action_id is not None:
            action_id = spec.action_id
            prop_key = spec.prop_key
        elif socketry is not None:
            # Fallback: use socketry to resolve unknown slugs (e.g. future device props).
            from socketry.properties import resolve as _socketry_resolve
            setting = _socketry_resolve(property_slug)
            if setting is None or setting.action_id is None:
                raise KeyError(f"Unknown or read-only property slug: {property_slug!r}")
            action_id = setting.action_id
            prop_key = setting.prop_key
        else:
            raise KeyError(f"Cannot resolve property slug without socketry: {property_slug!r}")

        body = {prop_key: int(value)}
        await self.async_send_device_command(device_id, device_sn, action_id, body)

    def _build_mqtt_params(self) -> tuple[dict, str]:
        """Build aiomqtt connection params from REST API login credentials.

        Returns ``(params_dict, user_id)``.
        """
        if _socketry_mqtt_params is None:
            raise RuntimeError("socketry is not installed")
        if not self._mqtt_user_id or not self._mqtt_password_b64:
            raise RuntimeError("MQTT credentials not available - call login() first")
        creds = {
            "userId": self._mqtt_user_id,
            "mqttPassWord": self._mqtt_password_b64,
            "macId": self._mac_id,
        }
        return _socketry_mqtt_params(creds), self._mqtt_user_id

    async def async_send_device_command(
        self,
        device_id: str,
        device_sn: str,
        action_id: int,
        body: dict,
        message_type: str = "DevicePropertyChange",
        *,
        verify: bool = True,
    ) -> dict:
        """Send an MQTT command and wait for the device to confirm it.

        A command only counts as done when the device replies. With
        ``verify`` (the default), the reply must also report every commanded
        value (all body keys except the ``cmd`` opcode); a different value
        means the device refused it. Callers whose reply cannot be matched
        key-for-key (circuit toggles, plan CRUD) pass ``verify=False`` and
        accept any reply for the same action.

        Returns the reply. Raises JackeryCommandUnconfirmed on timeout,
        JackeryCommandRejected on a mismatched value, and
        JackeryCommandError if the command could not be sent at all.
        """
        if aiomqtt is None:
            raise RuntimeError("aiomqtt is not installed")
        if self._mqtt_session is None:
            raise RuntimeError("MQTT session not started - call start_mqtt_session() first")
        self._raise_if_yielding()

        ts = int(time.time() * 1000)
        payload = {
            "deviceSn": device_sn,
            "id": ts,
            "version": 0,
            "messageType": message_type,
            "actionId": action_id,
            "timestamp": ts,
            "body": body,
        }
        _LOGGER.info("MQTT publish: actionId=%d body=%s", action_id, body)

        # Accept a response echoing the same actionId from this device.
        # Periodic push messages (actionId=1) are implicitly excluded.
        def _match(data: dict) -> bool:
            return (
                data.get("deviceSn") == device_sn
                and data.get("actionId") == action_id
            )

        try:
            result = await self._mqtt_session.publish_and_wait(
                payload, _match, timeout=COMMAND_CONFIRM_TIMEOUT_SEC
            )
        except TimeoutError:
            raise JackeryCommandUnconfirmed(
                f"device did not confirm within {COMMAND_CONFIRM_TIMEOUT_SEC:.0f}s; "
                "it may or may not have applied"
            ) from None
        except Exception as err:
            raise JackeryCommandError(
                f"command could not be sent ({type(err).__name__}: {err})"
            ) from err

        reply = result.get("body")
        _LOGGER.info(
            "MQTT response: messageType=%s actionId=%s body=%s",
            result.get("messageType"),
            result.get("actionId"),
            _redact(reply),
        )
        if verify and _commanded_values(body):
            if _reply_reports_values(body, reply):
                _verify_reply(body, reply)
            else:
                await self._async_confirm_by_readback(device_id, body)
        return result

    async def _async_confirm_by_readback(self, device_id: str, body: dict) -> None:
        """Confirm an acknowledged command by reading the device state back."""
        expected = _commanded_values(body)
        reported: dict = {}
        for delay in READBACK_DELAYS_SEC:
            await asyncio.sleep(delay)
            try:
                detail = await asyncio.to_thread(self.get_device_detail, device_id)
            except JackerySessionYielded:
                raise
            except Exception as err:  # noqa: BLE001 - report, don't guess
                raise JackeryCommandUnconfirmed(
                    "device acknowledged the command, but its state could not be "
                    f"read back to check it ({type(err).__name__})"
                ) from None
            props = ((detail.get("data") or {}).get("properties")) or {}
            missing = [k for k in expected if k not in props]
            if missing:
                raise JackeryCommandUnconfirmed(
                    "device acknowledged the command, but does not report "
                    f"{', '.join(missing)} so it can't be checked"
                )
            reported = {k: props[k] for k in expected}
            if all(_same_value(v, reported[k]) for k, v in expected.items()):
                return
        detail = ", ".join(
            f"{k}: sent {v}, device still reports {reported[k]}"
            for k, v in expected.items()
            if not _same_value(v, reported[k])
        )
        total = sum(READBACK_DELAYS_SEC)
        raise JackeryCommandRejected(
            f"device acknowledged the command but did not apply it after "
            f"{total:.0f}s ({detail})"
        )

    async def async_set_device_dp(
        self,
        device_id: str,
        device_sn: str,
        dp_id: str | int,
        value: str | int | bool,
    ) -> None:
        """Set a raw device DP through the existing control channel."""
        await self.async_set_device_property(
            device_id,
            device_sn,
            str(dp_id),
            value,
        )

    @staticmethod
    def _resolve_control_device(client, device_id: str, device_sn: str):
        """Resolve a controllable device from the cached Socketry device list."""
        for device in client.devices:
            if str(device.get("devId", "")) == str(device_id):
                return client.device(str(device["devSn"]))
            if device_sn and str(device.get("devSn", "")) == str(device_sn):
                return client.device(str(device["devSn"]))

        raise KeyError(
            f"Unable to resolve Jackery device for control (device_id={device_id}, "
            f"device_sn={device_sn})."
        )

    async def async_query_transfer_switch_plans(
        self,
        device_sn: str,
    ) -> list[dict] | None:
        """Query charge/discharge plans from the Transfer Switch.

        Returns the device's plan list - possibly empty, which is a real
        answer ("no plans") - or None when no answer arrived. Upstream returned
        [] for both, so callers kept a stale cache whenever the last plan was
        deleted.
        """
        if aiomqtt is None:
            raise RuntimeError("aiomqtt is not installed")
        if self._mqtt_session is None:
            raise RuntimeError("MQTT session not started - call start_mqtt_session() first")

        ts = int(time.time() * 1000)
        payload = {
            "deviceSn": device_sn,
            "id": ts,
            "version": 0,
            "messageType": "QueryElectricityStrategy",
            "actionId": 12,
            "timestamp": ts,
            "body": {"cmd": 15},
        }

        def _match(data: dict) -> bool:
            return (
                data.get("deviceSn") == device_sn
                and isinstance(data.get("body"), dict)
                and "cds" in data["body"]
            )

        try:
            result = await self._mqtt_session.publish_and_wait(payload, _match, timeout=10.0)
            cds = result["body"]["cds"]
            return list(cds) if isinstance(cds, list) else None
        except TimeoutError:
            _LOGGER.warning("Timeout waiting for plan query response from %s", device_sn)
        except Exception:
            _LOGGER.exception("Failed to query plans for %s", device_sn)
        return None

    async def _async_confirm_plans(
        self, device_sn: str, applied, what: str
    ) -> list[dict]:
        """Re-read the plan list until ``applied(plans)`` holds; return it.

        Plan commands are only acknowledged, never echoed, so like other
        Transfer Switch commands they are confirmed by reading state back.
        """
        plans = None
        answered = False
        for delay in READBACK_DELAYS_SEC:
            await asyncio.sleep(delay)
            plans = await self.async_query_transfer_switch_plans(device_sn)
            if plans is None:
                continue
            answered = True
            if applied(plans):
                return plans
        if not answered:
            raise JackeryCommandUnconfirmed(
                f"{what}: the Transfer Switch acknowledged it, but its plan list "
                "could not be read back to check"
            )
        raise JackeryCommandRejected(
            f"{what}: the Transfer Switch acknowledged it, but the plan list "
            f"did not change after {sum(READBACK_DELAYS_SEC):.0f}s"
        )

    async def async_update_transfer_switch_plan(
        self,
        device_id: str,
        device_sn: str,
        plan: dict,
    ) -> list[dict]:
        """Update an existing plan; returns the confirmed plan list."""
        await self.async_send_device_command(
            device_id,
            device_sn,
            14,  # actionId for UpdateElectricityStrategy
            {"cmd": 17, **plan},
            message_type="UpdateElectricityStrategy",
            verify=False,
        )
        pid = str(plan.get("pid"))
        fields = {k: v for k, v in plan.items() if k != "pid"}

        def applied(plans: list[dict]) -> bool:
            for p in plans:
                if str(p.get("pid")) == pid:
                    return all(_same_value(v, p.get(k)) for k, v in fields.items())
            return False

        return await self._async_confirm_plans(device_sn, applied, f"Update plan {pid}")

    async def async_create_transfer_switch_plan(
        self,
        device_id: str,
        device_sn: str,
        plan: dict,
    ) -> list[dict]:
        """Create a plan; returns the confirmed plan list."""
        before = await self.async_query_transfer_switch_plans(device_sn)
        before_pids = {str(p.get("pid")) for p in before or []}
        await self.async_send_device_command(
            device_id,
            device_sn,
            13,  # actionId for InsertElectricityStrategy
            {"cmd": 16, **plan},
            message_type="InsertElectricityStrategy",
            verify=False,
        )
        match_keys = [k for k in ("tt", "st", "et") if k in plan]

        def applied(plans: list[dict]) -> bool:
            return any(
                str(p.get("pid")) not in before_pids
                and all(_same_value(plan[k], p.get(k)) for k in match_keys)
                for p in plans
            )

        return await self._async_confirm_plans(device_sn, applied, "Create plan")

    async def async_delete_transfer_switch_plan(
        self,
        device_id: str,
        device_sn: str,
        pid: str,
    ) -> list[dict]:
        """Delete a plan; returns the confirmed plan list."""
        await self.async_send_device_command(
            device_id,
            device_sn,
            15,  # actionId for DeleteElectricityStrategy
            {"cmd": 18, "pid": pid},
            message_type="DeleteElectricityStrategy",
            verify=False,
        )

        def applied(plans: list[dict]) -> bool:
            return all(str(p.get("pid")) != str(pid) for p in plans)

        return await self._async_confirm_plans(device_sn, applied, f"Delete plan {pid}")

    async def async_query_transfer_switch_circuits(
        self,
        device_sn: str,
    ) -> list[dict]:
        """Query circuit properties from Transfer Switch via persistent MQTT session."""
        if aiomqtt is None:
            raise RuntimeError("aiomqtt is not installed")
        if self._mqtt_session is None:
            raise RuntimeError("MQTT session not started - call start_mqtt_session() first")

        ts = int(time.time() * 1000)
        payload = {
            "deviceSn": device_sn,
            "id": ts,
            "version": 0,
            "messageType": "QueryCircuitProperty",
            "actionId": 7,
            "timestamp": ts,
            "body": {"cmd": 10},
        }

        def _match(data: dict) -> bool:
            # Only accept actionId=7 (full QueryCircuitProperty response).
            # actionId=1 are unsolicited partial power-only pushes - ignore them.
            return (
                data.get("deviceSn") == device_sn
                and data.get("actionId") == 7
                and isinstance(data.get("body"), dict)
                and "cir" in data["body"]
            )

        try:
            result = await self._mqtt_session.publish_and_wait(payload, _match, timeout=10.0)
            return result["body"]["cir"]
        except TimeoutError:
            _LOGGER.warning("Timeout waiting for circuit query response from %s", device_sn)
        except Exception:
            _LOGGER.exception("Failed to query circuits for %s", device_sn)
        return []

    async def async_set_circuit_switch(
        self,
        device_id: str,
        device_sn: str,
        idx: int,
        on: bool,
    ) -> None:
        """Toggle a circuit on/off on the Transfer Switch."""
        await self.async_send_device_command(
            device_id,
            device_sn,
            9,  # actionId for circuit switch
            {"cmd": 12, "idx": idx, "sw": 1 if on else 0},
            verify=False,
        )
