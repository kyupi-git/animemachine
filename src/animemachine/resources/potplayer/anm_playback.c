/* AnimeMachine optional PotPlayer bridge. AGPL-3.0-only.
 * Runs in its own player process; no files, history scraping or helper process.
 * Build: Zig 0.14.1 cc -target x86_64-windows-gnu -shared -O2 -Wall -Wextra
 * -Werror -s -o anm_playback.dll anm_playback.c -luser32 -lwinhttp
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winhttp.h>
#include <wchar.h>
#include <stdio.h>

typedef struct { wchar_t marker[80]; wchar_t url[4096]; int resume_ms; } Candidate;
static Candidate candidates[1024];
static int count;
static SRWLOCK candidate_lock = SRWLOCK_INIT;
static volatile LONG stopping, running;
static HMODULE self;
static LONG event_number;
static char instance_id[64];

static BOOL CALLBACK find_player(HWND window, LPARAM destination) {
    DWORD pid = 0;
    wchar_t name[64];
    GetWindowThreadProcessId(window, &pid);
    if (pid == GetCurrentProcessId() && GetClassNameW(window, name, 64)
        && wcscmp(name, L"PotPlayer64") == 0) {
        *(HWND *)destination = window;
        return FALSE;
    }
    return TRUE;
}

static LRESULT query(HWND window, WPARAM code) {
    DWORD_PTR result = 0;
    if (!SendMessageTimeoutW(window, WM_USER, code, 0, SMTO_ABORTIFHUNG, 120, &result)) return -999;
    return (LRESULT)result;
}

static BOOL report(const wchar_t *url, LONG event, LRESULT position, LRESULT duration) {
    URL_COMPONENTS parts = {0};
    parts.dwStructSize = sizeof(parts);
    parts.dwHostNameLength = parts.dwUrlPathLength = parts.dwUserNameLength = parts.dwExtraInfoLength = (DWORD)-1;
    if (!WinHttpCrackUrl(url, 0, 0, &parts) || parts.dwUserNameLength || parts.dwExtraInfoLength
        || parts.dwHostNameLength >= 256 || parts.dwUrlPathLength >= 4096
        || (parts.nScheme != INTERNET_SCHEME_HTTP && parts.nScheme != INTERNET_SCHEME_HTTPS)) return FALSE;
    wchar_t host[256], path[4096];
    wmemcpy(host, parts.lpszHostName, parts.dwHostNameLength); host[parts.dwHostNameLength] = 0;
    wmemcpy(path, parts.lpszUrlPath, parts.dwUrlPathLength); path[parts.dwUrlPathLength] = 0;
    if (wcsncmp(path, L"/api/playback/started/", 22) != 0) return FALSE;
    HINTERNET session = WinHttpOpen(L"AnimeMachine-PotPlayer/1", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, NULL, NULL, 0);
    if (!session) return FALSE;
    WinHttpSetTimeouts(session, 500, 1000, 1000, 1000);
    HINTERNET connection = WinHttpConnect(session, host, parts.nPort, 0);
    HINTERNET request = connection ? WinHttpOpenRequest(connection, L"POST", path, NULL, WINHTTP_NO_REFERER,
        WINHTTP_DEFAULT_ACCEPT_TYPES, parts.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0) : NULL;
    BOOL success = FALSE;
    if (request) {
        DWORD redirect = WINHTTP_OPTION_REDIRECT_POLICY_NEVER;
        WinHttpSetOption(request, WINHTTP_OPTION_REDIRECT_POLICY, &redirect, sizeof(redirect));
        char body[240];
        int length = snprintf(body, sizeof(body), "{\"event\":%ld,\"positionMs\":%lld,\"durationMs\":%lld,\"instance\":\"%s\"}",
                              event, (long long)position, (long long)duration, instance_id);
        if (WinHttpSendRequest(request, L"Content-Type: application/json\r\n", (DWORD)-1,
                              body, (DWORD)length, (DWORD)length, 0) && WinHttpReceiveResponse(request, NULL)) {
            DWORD code = 0, size = sizeof(code);
            success = WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                                         NULL, &code, &size, NULL) && code == 200;
        }
        WinHttpCloseHandle(request);
    }
    if (connection) WinHttpCloseHandle(connection);
    WinHttpCloseHandle(session);
    return success;
}

static DWORD WINAPI observe(void *unused) {
    (void)unused;
    wchar_t prior[80] = L"", sought[80] = L"";
    LRESULT previous_position = -1;
    Candidate active = {0};
    LRESULT last_position = 0, last_duration = 0, last_state = 0;
    ULONGLONG last_report = 0;
    while (!InterlockedCompareExchange(&stopping, 0, 0)) {
        HWND window = NULL;
        EnumWindows(find_player, (LPARAM)&window);
        wchar_t title[2048] = L"";
        if (window) GetWindowTextW(window, title, 2048);
        LRESULT state = window ? query(window, 20486) : -999;
        LRESULT position = window ? query(window, 20484) : 0;
        LRESULT duration = window ? query(window, 20482) : 0;
        Candidate current = {0};
        int matches = 0;
        AcquireSRWLockShared(&candidate_lock);
        for (int i = 0; i < count; i++) if (wcsstr(title, candidates[i].marker)) {
            matches++; current = candidates[i];
        }
        ReleaseSRWLockShared(&candidate_lock);
        BOOL same = matches == 1 && !wcscmp(active.marker, current.marker);
        if (active.marker[0] && (!same || state == 0 || (last_state == 2 && state == 1))) {
            report(active.url, InterlockedIncrement(&event_number), last_position, last_duration);
            last_report = GetTickCount64();
            if (!same || state == 0) active.marker[0] = 0;
        }
        if (matches == 1 && state == 2 && position >= 0 && duration > 0) {
            if (wcscmp(sought, current.marker)) {
                wcscpy(sought, current.marker);
                if (current.resume_ms > 0 && current.resume_ms < duration) {
                    DWORD_PTR result = 0;
                    SendMessageTimeoutW(window, WM_USER, 20485, current.resume_ms, SMTO_ABORTIFHUNG, 200, &result);
                    prior[0] = 0; previous_position = -1;
                    Sleep(200);
                    continue;
                }
            }
            if (!wcscmp(prior, current.marker) && position > previous_position) {
                BOOL first = !active.marker[0];
                active = current;
                last_position = position; last_duration = duration;
                if (first || GetTickCount64() - last_report >= 2000) {
                LONG event = InterlockedIncrement(&event_number);
                for (int attempt = 0; attempt < 3 && !InterlockedCompareExchange(&stopping, 0, 0); attempt++) {
                    if (report(current.url, event, position, duration)) break;
                    Sleep((DWORD)(200 * (attempt + 1)));
                }
                last_report = GetTickCount64();
                }
            }
            wcscpy(prior, current.marker); previous_position = position;
        } else { prior[0] = 0; previous_position = -1; }
        if (same && state == 1 && position >= 0) last_position = position;
        last_state = state;
        Sleep(200);
    }
    if (active.marker[0]) report(active.url, InterlockedIncrement(&event_number), last_position, last_duration);
    FreeLibraryAndExitThread(self, 0);
    return 0;
}

__declspec(dllexport) int RegisterCandidate(const char *marker, const char *url, int resume_ms) {
    if (!marker || !url || strlen(marker) >= 80 || strlen(url) >= 4096) return 0;
    Candidate candidate = {0};
    candidate.resume_ms = resume_ms >= 0 && resume_ms <= 604800000 ? resume_ms : 0;
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, marker, -1, candidate.marker, 80)
        || !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, url, -1, candidate.url, 4096)) return 0;
    AcquireSRWLockExclusive(&candidate_lock);
    int index = -1;
    for (int i = 0; i < count; i++) if (!wcscmp(candidates[i].marker, candidate.marker)) index = i;
    if (index < 0 && count < 1024) index = count++;
    if (index >= 0) candidates[index] = candidate;
    ReleaseSRWLockExclusive(&candidate_lock);
    return index >= 0;
}

__declspec(dllexport) int Start(int unused) {
    (void)unused;
    if (InterlockedCompareExchange(&running, 1, 0)) return !InterlockedCompareExchange(&stopping, 0, 0);
    FILETIME stamp;
    GetSystemTimeAsFileTime(&stamp);
    ULARGE_INTEGER identifier;
    identifier.LowPart = stamp.dwLowDateTime; identifier.HighPart = stamp.dwHighDateTime;
    snprintf(instance_id, sizeof(instance_id), "%llx-%lu", identifier.QuadPart, GetCurrentProcessId());
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS, (LPCWSTR)&Start, &self)) return 0;
    HANDLE thread = CreateThread(NULL, 0, observe, NULL, 0, NULL);
    if (!thread) { FreeLibrary(self); return 0; }
    CloseHandle(thread);
    return 1;
}

__declspec(dllexport) int Stop(int unused) {
    (void)unused;
    InterlockedExchange(&stopping, 1);
    return 1;
}
