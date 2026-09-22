"""Declarations ctypes des API Windows utilisees par le tracker.

Pourquoi ctypes et pas pywin32 : aucune extension compilee a installer,
donc rien que la strategie de controle d'application de la machine puisse
bloquer (le sort de scipy dans Chronicle, cf. README).

Chaque fonction a ses argtypes/restype explicites. Sans eux, ctypes passe
les entiers en 32 bits : sur Windows 64 bits, un HWND ou un LPARAM tronque
fait planter le processus, ou pire, marche presque.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
version = ctypes.WinDLL("version", use_last_error=True)
pdh = ctypes.WinDLL("pdh", use_last_error=True)
iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
HANDLE = wt.HANDLE
HWND = wt.HWND

# --------------------------------------------------------------- messages
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_TIMER = 0x0113
WM_INPUT = 0x00FF
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
WM_APP = 0x8000

ENDSESSION_LOGOFF = 0x80000000

PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

WTS_SESSION_LOGON = 0x5
WTS_SESSION_LOGOFF = 0x6
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0

# ----------------------------------------------------------- win events
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0
CHILDID_SELF = 0

# ------------------------------------------------------------ raw input
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
MOUSE_MOVE_ABSOLUTE = 0x01
RI_KEY_BREAK = 0x01
RI_MOUSE_BUTTONS_DOWN = 0x0001 | 0x0004 | 0x0010 | 0x0040 | 0x0100
RI_MOUSE_WHEEL = 0x0400
RI_MOUSE_HWHEEL = 0x0800

# ------------------------------------------------------------- processus
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102

# ---------------------------------------------------------------- divers
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
ERROR_ALREADY_EXISTS = 183
ERROR_OPERATION_ABORTED = 995

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, wt.UINT, WPARAM, LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, HANDLE, wt.DWORD, HWND, wt.LONG,
                                  wt.LONG, wt.DWORD, wt.DWORD)
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, HWND, LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", HWND), ("message", wt.UINT), ("wParam", WPARAM),
                ("lParam", LPARAM), ("time", wt.DWORD), ("pt", wt.POINT),
                ("lPrivate", wt.DWORD)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", HANDLE), ("wParam", WPARAM)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]

    @property
    def value(self) -> int:
        return (self.dwHighDateTime << 32) | self.dwLowDateTime


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("DeviceName", wt.WCHAR * 32),
                ("DeviceString", wt.WCHAR * 128), ("StateFlags", wt.DWORD),
                ("DeviceID", wt.WCHAR * 128), ("DeviceKey", wt.WCHAR * 128)]


def _sig(fonction, restype, *argtypes):
    fonction.restype = restype
    fonction.argtypes = list(argtypes)
    return fonction


# ---------------------------------------------------------------- user32
_sig(user32.RegisterClassExW, wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(user32.UnregisterClassW, wt.BOOL, wt.LPCWSTR, wt.HINSTANCE)
_sig(user32.CreateWindowExW, HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR,
     wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
_sig(user32.DestroyWindow, wt.BOOL, HWND)
_sig(user32.DefWindowProcW, LRESULT, HWND, wt.UINT, WPARAM, LPARAM)
_sig(user32.GetMessageW, wt.BOOL, ctypes.POINTER(MSG), HWND, wt.UINT,
     wt.UINT)
_sig(user32.TranslateMessage, wt.BOOL, ctypes.POINTER(MSG))
_sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(MSG))
_sig(user32.PostMessageW, wt.BOOL, HWND, wt.UINT, WPARAM, LPARAM)
_sig(user32.PostQuitMessage, None, ctypes.c_int)
_sig(user32.SetTimer, ctypes.c_size_t, HWND, ctypes.c_size_t, wt.UINT,
     wt.LPVOID)
_sig(user32.KillTimer, wt.BOOL, HWND, ctypes.c_size_t)
_sig(user32.GetForegroundWindow, HWND)
_sig(user32.GetWindowTextLengthW, ctypes.c_int, HWND)
_sig(user32.GetWindowTextW, ctypes.c_int, HWND, wt.LPWSTR, ctypes.c_int)
_sig(user32.GetClassNameW, ctypes.c_int, HWND, wt.LPWSTR, ctypes.c_int)
_sig(user32.GetWindowThreadProcessId, wt.DWORD, HWND,
     ctypes.POINTER(wt.DWORD))
_sig(user32.IsWindowVisible, wt.BOOL, HWND)
_sig(user32.IsWindow, wt.BOOL, HWND)
_sig(user32.GetWindow, HWND, HWND, wt.UINT)
# GetWindowLongPtrW n'existe qu'en 64 bits (c'est une macro en 32 bits).
GetWindowLongPtr = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
_sig(GetWindowLongPtr, ctypes.c_ssize_t, HWND, ctypes.c_int)
_sig(user32.EnumWindows, wt.BOOL, WNDENUMPROC, LPARAM)
_sig(user32.EnumChildWindows, wt.BOOL, HWND, WNDENUMPROC, LPARAM)
_sig(user32.SetWinEventHook, HANDLE, wt.DWORD, wt.DWORD, wt.HMODULE,
     WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD)
_sig(user32.UnhookWinEvent, wt.BOOL, HANDLE)
_sig(user32.RegisterRawInputDevices, wt.BOOL,
     ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT)
_sig(user32.GetRawInputData, wt.UINT, HANDLE, wt.UINT, wt.LPVOID,
     ctypes.POINTER(wt.UINT), wt.UINT)
_sig(user32.GetLastInputInfo, wt.BOOL, ctypes.POINTER(LASTINPUTINFO))
_sig(user32.EnumDisplayDevicesW, wt.BOOL, wt.LPCWSTR, wt.DWORD,
     ctypes.POINTER(DISPLAY_DEVICEW), wt.DWORD)

# -------------------------------------------------------------- wtsapi32
_sig(wtsapi32.WTSRegisterSessionNotification, wt.BOOL, HWND, wt.DWORD)
_sig(wtsapi32.WTSUnRegisterSessionNotification, wt.BOOL, HWND)

# -------------------------------------------------------------- kernel32
_sig(kernel32.GetModuleHandleW, wt.HMODULE, wt.LPCWSTR)
_sig(kernel32.GetCurrentThreadId, wt.DWORD)
_sig(kernel32.GetTickCount, wt.DWORD)
_sig(kernel32.OpenProcess, HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_sig(kernel32.CloseHandle, wt.BOOL, HANDLE)
_sig(kernel32.QueryFullProcessImageNameW, wt.BOOL, HANDLE, wt.DWORD,
     wt.LPWSTR, ctypes.POINTER(wt.DWORD))
_sig(kernel32.GetProcessTimes, wt.BOOL, HANDLE, ctypes.POINTER(FILETIME),
     ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
     ctypes.POINTER(FILETIME))
_sig(kernel32.WaitForSingleObject, wt.DWORD, HANDLE, wt.DWORD)
_sig(kernel32.CreateMutexW, HANDLE, wt.LPVOID, wt.BOOL, wt.LPCWSTR)
_sig(kernel32.CreateEventW, HANDLE, wt.LPVOID, wt.BOOL, wt.BOOL, wt.LPCWSTR)
_sig(kernel32.OpenEventW, HANDLE, wt.DWORD, wt.BOOL, wt.LPCWSTR)
_sig(kernel32.SetEvent, wt.BOOL, HANDLE)
_sig(kernel32.CreateFileW, HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID,
     wt.DWORD, wt.DWORD, HANDLE)
_sig(kernel32.ReadDirectoryChangesW, wt.BOOL, HANDLE, wt.LPVOID, wt.DWORD,
     wt.BOOL, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID, wt.LPVOID)
_sig(kernel32.CancelIoEx, wt.BOOL, HANDLE, wt.LPVOID)

# --------------------------------------------------------------- version
_sig(version.GetFileVersionInfoSizeW, wt.DWORD, wt.LPCWSTR,
     ctypes.POINTER(wt.DWORD))
_sig(version.GetFileVersionInfoW, wt.BOOL, wt.LPCWSTR, wt.DWORD, wt.DWORD,
     wt.LPVOID)
_sig(version.VerQueryValueW, wt.BOOL, wt.LPCVOID, wt.LPCWSTR,
     ctypes.POINTER(wt.LPVOID), ctypes.POINTER(wt.UINT))

INVALID_HANDLE_VALUE = HANDLE(-1).value


# --------------------------------------------------------------- outils

def window_text(hwnd) -> str:
    longueur = user32.GetWindowTextLengthW(hwnd)

    if longueur <= 0:
        return ""

    tampon = ctypes.create_unicode_buffer(longueur + 1)
    user32.GetWindowTextW(hwnd, tampon, longueur + 1)
    return tampon.value


def class_name(hwnd) -> str:
    tampon = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, tampon, 256)
    return tampon.value


def window_pid(hwnd) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def filetime_to_epoch(valeur: int) -> float:
    """FILETIME (100 ns depuis 1601) -> secondes epoch Unix."""
    return (valeur - 116444736000000000) / 10_000_000
