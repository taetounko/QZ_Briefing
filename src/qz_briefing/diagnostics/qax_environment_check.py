"""Validate the Windows PyQt5 and QAxWidget runtime environment."""

from __future__ import annotations

import platform
import struct
import sys
import traceback


def main() -> int:
    """Run the environment checks without showing a window or an event loop."""
    python_version = platform.python_version()
    architecture_bits = struct.calcsize("P") * 8
    platform_name = platform.system()

    checks = {
        "PyQt5 import": False,
        "QAxWidget import": False,
        "QApplication creation": False,
        "QAxWidget creation": False,
    }
    application = None
    qax_widget = None
    owns_application = False
    error_details: list[str] = []

    try:
        if platform_name != "Windows":
            raise RuntimeError(f"Windows is required, detected {platform_name}")
        if sys.version_info[:2] != (3, 11):
            raise RuntimeError(f"Python 3.11 is required, detected {python_version}")
        if architecture_bits != 32:
            raise RuntimeError(
                f"32-bit Python is required, detected {architecture_bits}-bit"
            )

        from PyQt5 import QtCore
        from PyQt5.QtWidgets import QApplication

        _ = QtCore.PYQT_VERSION_STR
        checks["PyQt5 import"] = True

        from PyQt5.QAxContainer import QAxWidget

        checks["QAxWidget import"] = True

        application = QApplication.instance()
        if application is None:
            application = QApplication([])
            owns_application = True
        checks["QApplication creation"] = True

        qax_widget = QAxWidget()
        checks["QAxWidget creation"] = True
    except Exception:
        error_details.append(traceback.format_exc().rstrip())
    finally:
        if qax_widget is not None:
            try:
                qax_widget.close()
                qax_widget.deleteLater()
            except Exception:
                error_details.append(traceback.format_exc().rstrip())

        if application is not None:
            try:
                application.processEvents()
                if owns_application:
                    application.quit()
            except Exception:
                error_details.append(traceback.format_exc().rstrip())

        qax_widget = None
        application = None

    environment_ok = (
        platform_name == "Windows"
        and sys.version_info[:2] == (3, 11)
        and architecture_bits == 32
    )
    overall_ok = environment_ok and all(checks.values()) and not error_details

    print(f"Python: {python_version}")
    print(f"Architecture: {architecture_bits}-bit")
    print(f"Platform: {platform_name}")
    for name, passed in checks.items():
        print(f"{name}: {'PASS' if passed else 'FAIL'}")
    print(f"Overall: {'PASS' if overall_ok else 'FAIL'}")

    if error_details:
        print("Error details:", file=sys.stderr)
        for details in error_details:
            print(details, file=sys.stderr)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
