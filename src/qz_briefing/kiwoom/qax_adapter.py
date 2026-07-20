"""QAxWidget adapter for the minimal Kiwoom connection protocol."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


KIWOOM_CONTROL_ID = "KHOPENAPI.KHOpenAPICtrl.1"
LoginEventListener = Callable[[int], None]


class KiwoomAdapterError(Exception):
    """Base error for the QAx connection adapter."""


class KiwoomAdapterConfigurationError(KiwoomAdapterError):
    """Raised when incompatible adapter construction options are supplied."""


class KiwoomControlBindingError(KiwoomAdapterError):
    """Raised when the Kiwoom ActiveX control cannot be bound."""


class KiwoomConnectionStateError(KiwoomAdapterError):
    """Raised when the OCX returns an invalid connection state."""


class KiwoomConnectionRequestError(KiwoomAdapterError):
    """Raised when the immediate connection request result is invalid."""


class KiwoomLoginEventError(KiwoomAdapterError):
    """Raised when a login event error code cannot be converted to an integer."""


class KiwoomAdapterClosedError(KiwoomAdapterError):
    """Raised when an operation is requested after adapter cleanup."""


class KiwoomMasterDataError(KiwoomAdapterError):
    """Raised when a read-only master-data value is unavailable."""


class SignalLike(Protocol):
    def connect(self, callback: LoginEventListener) -> None: ...

    def disconnect(self, callback: LoginEventListener) -> None: ...


class QAxWidgetLike(Protocol):
    OnEventConnect: SignalLike

    def setControl(self, control_id: str) -> bool: ...

    def isNull(self) -> bool: ...

    def dynamicCall(self, signature: str, *arguments: object) -> object: ...

    def close(self) -> object: ...

    def deleteLater(self) -> None: ...


WidgetFactory = Callable[[], QAxWidgetLike]


def _create_qax_widget() -> QAxWidgetLike:
    """Create and bind the real widget exactly as the verified login client does."""
    from PyQt5.QAxContainer import QAxWidget

    return QAxWidget(KIWOOM_CONTROL_ID)


class KiwoomQAxAdapter:
    """Expose connection-only Kiwoom OCX operations to the manager."""

    def __init__(
        self,
        widget: QAxWidgetLike | None = None,
        widget_factory: WidgetFactory | None = None,
    ) -> None:
        if widget is not None and widget_factory is not None:
            raise KiwoomAdapterConfigurationError(
                "Provide either widget or widget_factory, not both"
            )

        uses_default_factory = widget is None and widget_factory is None
        factory = widget_factory or _create_qax_widget
        self._widget = widget if widget is not None else factory()
        self._listeners: list[LoginEventListener] = []
        self._closed = False
        self._signal_connected = False
        self._listener_error_count = 0
        self._cleanup_error_count = 0
        self._connect_request_count = 0
        self._login_event_count = 0
        self._last_login_error_code: int | None = None
        self._last_connect_state: int | None = None
        self._signal_handler = self._handle_login_event

        try:
            if not uses_default_factory:
                binding_result = bool(self._widget.setControl(KIWOOM_CONTROL_ID))
                if not binding_result:
                    raise KiwoomControlBindingError(
                        f"setControl failed for {KIWOOM_CONTROL_ID}"
                    )
            if bool(self._widget.isNull()):
                raise KiwoomControlBindingError(
                    f"QAxWidget is null after binding {KIWOOM_CONTROL_ID}"
                )

            self._widget.OnEventConnect.connect(self._signal_handler)
            self._signal_connected = True
        except KiwoomAdapterError:
            self._closed = True
            self._dispose_widget()
            raise
        except Exception as exc:
            self._closed = True
            self._dispose_widget()
            raise KiwoomControlBindingError(
                "Kiwoom control binding or event signal setup failed"
            ) from exc

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def listener_error_count(self) -> int:
        return self._listener_error_count

    @property
    def cleanup_error_count(self) -> int:
        return self._cleanup_error_count

    @property
    def connect_request_count(self) -> int:
        return self._connect_request_count

    @property
    def login_event_count(self) -> int:
        return self._login_event_count

    @property
    def last_login_error_code(self) -> int | None:
        return self._last_login_error_code

    @property
    def last_connect_state(self) -> int | None:
        return self._last_connect_state

    def get_connect_state(self) -> int:
        """Return only the valid Kiwoom connection states 0 and 1."""
        self._ensure_open()
        try:
            raw_state = self._widget.dynamicCall("GetConnectState()")
            connect_state = int(raw_state)
        except Exception as exc:
            raise KiwoomConnectionStateError(
                "GetConnectState did not return an integer"
            ) from exc

        if connect_state not in (0, 1):
            raise KiwoomConnectionStateError(
                f"GetConnectState returned invalid state {connect_state}"
            )
        self._last_connect_state = connect_state
        return connect_state

    def request_connect(self) -> int:
        """Issue exactly one immediate connection request and return its result."""
        self._ensure_open()
        if self._connect_request_count >= 1:
            raise KiwoomConnectionRequestError(
                "CommConnect was already requested by this adapter"
            )
        self._connect_request_count += 1
        try:
            raw_result = self._widget.dynamicCall("CommConnect()")
            return int(raw_result)
        except Exception as exc:
            raise KiwoomConnectionRequestError(
                "CommConnect did not return an integer"
            ) from exc

    def get_master_code_name(self, code: str) -> str:
        """Return the listed security name using a read-only master query."""
        return self._get_required_master_text("GetMasterCodeName(QString)", code)

    def get_master_last_price(self, code: str) -> str:
        """Return the raw reference/previous-close price master value."""
        return self._get_required_master_text("GetMasterLastPrice(QString)", code)

    def add_login_event_listener(self, callback: LoginEventListener) -> None:
        """Register a callback once without storing authentication data."""
        self._ensure_open()
        if not callable(callback):
            raise KiwoomAdapterConfigurationError("Listener must be callable")
        if callback not in self._listeners:
            self._listeners.append(callback)

    def close(self) -> None:
        """Disconnect the signal and safely release the widget once."""
        if self._closed:
            return

        self._closed = True
        if self._signal_connected:
            try:
                self._widget.OnEventConnect.disconnect(self._signal_handler)
            except Exception:
                self._cleanup_error_count += 1
            self._signal_connected = False

        self._listeners.clear()
        self._dispose_widget()

    def _handle_login_event(self, raw_error_code: object) -> None:
        try:
            error_code = int(raw_error_code)
        except Exception as exc:
            raise KiwoomLoginEventError(
                "OnEventConnect did not provide an integer error code"
            ) from exc

        self._login_event_count += 1
        self._last_login_error_code = error_code

        for callback in tuple(self._listeners):
            try:
                callback(error_code)
            except Exception:
                self._listener_error_count += 1

    def _ensure_open(self) -> None:
        if self._closed:
            raise KiwoomAdapterClosedError("Kiwoom QAx adapter is closed")

    def _get_required_master_text(self, signature: str, code: str) -> str:
        self._ensure_open()
        normalized_code = str(code).strip()
        if not normalized_code:
            raise KiwoomMasterDataError("Security code must not be empty")
        try:
            raw_value = self._widget.dynamicCall(signature, normalized_code)
        except Exception as exc:
            raise KiwoomMasterDataError(
                f"{signature.split('(')[0]} failed for {normalized_code}"
            ) from exc
        value = str(raw_value).strip()
        if not value:
            raise KiwoomMasterDataError(
                f"{signature.split('(')[0]} returned an empty value for "
                f"{normalized_code}"
            )
        return value

    def _dispose_widget(self) -> None:
        try:
            self._widget.close()
        except Exception:
            self._cleanup_error_count += 1
        try:
            self._widget.deleteLater()
        except Exception:
            self._cleanup_error_count += 1
