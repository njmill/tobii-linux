#include <windows.h>
#include <stdio.h>
#include <stdint.h>

static void print_escaped(const char *text)
{
    putchar('"');
    if (text) {
        for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
            if (*p == '"' || *p == '\\')
                putchar('\\');
            if (*p >= 0x20 && *p < 0x7f)
                putchar(*p);
            else
                printf("\\x%02x", *p);
        }
    }
    putchar('"');
}

static void process_image(DWORD pid, char *buf, DWORD len)
{
    if (!buf || len == 0)
        return;
    buf[0] = 0;
    HANDLE proc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!proc)
        return;
    DWORD size = len;
    if (!QueryFullProcessImageNameA(proc, 0, buf, &size))
        buf[0] = 0;
    CloseHandle(proc);
}

static void print_rect(const char *name, const RECT *r)
{
    printf(" %s=%ld,%ld,%ld,%ld", name, (long)r->left, (long)r->top, (long)r->right, (long)r->bottom);
}

static BOOL CALLBACK enum_window(HWND hwnd, LPARAM lparam)
{
    (void)lparam;
    char title[512];
    char cls[256];
    char exe[MAX_PATH * 2];
    RECT rect = {0};
    RECT client = {0};
    POINT client_origin = {0, 0};
    DWORD pid = 0;
    DWORD tid = GetWindowThreadProcessId(hwnd, &pid);
    LONG_PTR style = GetWindowLongPtrA(hwnd, GWL_STYLE);
    LONG_PTR exstyle = GetWindowLongPtrA(hwnd, GWL_EXSTYLE);
    int visible = IsWindowVisible(hwnd) ? 1 : 0;

    title[0] = 0;
    cls[0] = 0;
    GetWindowTextA(hwnd, title, (int)sizeof(title));
    GetClassNameA(hwnd, cls, (int)sizeof(cls));
    GetWindowRect(hwnd, &rect);
    GetClientRect(hwnd, &client);
    ClientToScreen(hwnd, &client_origin);
    client.right += client_origin.x;
    client.left += client_origin.x;
    client.bottom += client_origin.y;
    client.top += client_origin.y;
    process_image(pid, exe, (DWORD)sizeof(exe));

    HMONITOR monitor = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
    MONITORINFOEXA mi;
    ZeroMemory(&mi, sizeof(mi));
    mi.cbSize = sizeof(mi);
    if (monitor)
        GetMonitorInfoA(monitor, (MONITORINFO *)&mi);

    printf("wine_window hwnd=%p visible=%d pid=%lu tid=%lu style=0x%08lx exstyle=0x%08lx",
           hwnd, visible, (unsigned long)pid, (unsigned long)tid,
           (unsigned long)style, (unsigned long)exstyle);
    print_rect("rect", &rect);
    print_rect("client", &client);
    if (monitor) {
        print_rect("monitor", &mi.rcMonitor);
        print_rect("work", &mi.rcWork);
        printf(" monitor_name=");
        print_escaped(mi.szDevice);
    }
    printf(" class=");
    print_escaped(cls);
    printf(" title=");
    print_escaped(title);
    printf(" exe=");
    print_escaped(exe);
    printf("\n");
    return TRUE;
}

int main(void)
{
    printf("wine_window_probe_begin\n");
    printf("virtual_screen=%d,%d,%d,%d screen=%d,%d\n",
           GetSystemMetrics(SM_XVIRTUALSCREEN),
           GetSystemMetrics(SM_YVIRTUALSCREEN),
           GetSystemMetrics(SM_CXVIRTUALSCREEN),
           GetSystemMetrics(SM_CYVIRTUALSCREEN),
           GetSystemMetrics(SM_CXSCREEN),
           GetSystemMetrics(SM_CYSCREEN));
    EnumWindows(enum_window, 0);
    printf("wine_window_probe_end\n");
    return 0;
}
