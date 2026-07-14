"""Validate Kiwoom OpenAPI+ OCX binding and read its connection state."""

from __future__ import annotations

import platform
import struct
import sys


CONTROL_ID = "KHOPENAPI.KHOpenAPICtrl.1"


def main() -> int:
    """Run the OCX check without showing a window or starting an event loop."""
    python_version = platform.python_version()
    architecture_bits = struct.calcsize("P") * 8
    platform_name = platform.system()

    application = None
    qax_widget = None
    owns_application = False
    control_binding = False
    widget_is_null: bool | None = None
    api_module_path = ""
    connect_state_call = False
    connection_state: int | None = None
    errors: list[tuple[str, str]] = []

    try:
        if platform_name != "Windows":
            raise RuntimeError(f"Windows is required, detected {platform_name}")
        if sys.version_info[:2] != (3, 11):
            raise RuntimeError(f"Python 3.11 is required, detected {python_version}")
        if architecture_bits != 32:
            raise RuntimeError(
                f"32-bit Python is required, detected {architecture_bits}-bit"
            )

        from PyQt5.QAxContainer import QAxWidget
        from PyQt5.QtWidgets import QApplication

        application = QApplication.instance()
        if application is None:
            application = QApplication([])
            owns_application = True

        qax_widget = QAxWidget()
        binding_result = bool(qax_widget.setControl(CONTROL_ID))
        widget_is_null = bool(qax_widget.isNull())
        control_binding = binding_result and not widget_is_null
        if not control_binding:
            raise RuntimeError(
                "Kiwoom OpenAPI+ control binding failed "
                f"(setControl={binding_result}, isNull={widget_is_null})"
            )

        raw_module_path = qax_widget.dynamicCall("GetAPIModulePath()")
        if raw_module_path is None:
            raise RuntimeError("GetAPIModulePath returned None")
        api_module_path = str(raw_module_path).strip()
        if not api_module_path:
            raise RuntimeError("GetAPIModulePath returned an empty path")

        raw_connection_state = qax_widget.dynamicCall("GetConnectState()")
        if raw_connection_state is None:
            raise RuntimeError("GetConnectState returned None")
        try:
            connection_state = int(raw_connection_state)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"GetConnectState returned a non-integer value: {raw_connection_state!r}"
            ) from exc
        if connection_state not in (0, 1):
            raise RuntimeError(
                f"GetConnectState returned an unexpected value: {connection_state}"
            )
        connect_state_call = True
    except Exception as exc:
        errors.append((type(exc).__name__, str(exc)))
    finally:
        if qax_widget is not None:
            try:
                qax_widget.clear()
                qax_widget.close()
                qax_widget.deleteLater()
            except Exception as exc:
                errors.append((type(exc).__name__, str(exc)))

        if application is not None:
            try:
                application.processEvents()
                if owns_application:
                    application.quit()
            except Exception as exc:
                errors.append((type(exc).__name__, str(exc)))

        qax_widget = None
        application = None

    connection_description = {
        0: "NOT_CONNECTED",
        1: "CONNECTED",
    }.get(connection_state, "UNKNOWN")
    environment_ok = (
        platform_name == "Windows"
        and sys.version_info[:2] == (3, 11)
        and architecture_bits == 32
    )
    overall_ok = (
        environment_ok
        and control_binding
        and bool(api_module_path)
        and connect_state_call
        and connection_state in (0, 1)
        and not errors
    )

    print(f"Python: {python_version}")
    print(f"Architecture: {architecture_bits}-bit")
    print(f"Platform: {platform_name}")
    print(f"Kiwoom control ID: {CONTROL_ID}")
    print(f"Control binding: {'PASS' if control_binding else 'FAIL'}")
    print(
        "QAxWidget isNull: "
        f"{widget_is_null if widget_is_null is not None else 'UNKNOWN'}"
    )
    print(f"API module path: {api_module_path or 'UNAVAILABLE'}")
    print(f"GetConnectState call: {'PASS' if connect_state_call else 'FAIL'}")
    print(
        "Connection state: "
        f"{connection_state if connection_state is not None else 'UNKNOWN'}"
    )
    print(f"Connection description: {connection_description}")
    print(f"Overall: {'PASS' if overall_ok else 'FAIL'}")

    for error_type, error_message in errors:
        print(f"Error: {error_type}: {error_message}", file=sys.stderr)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
