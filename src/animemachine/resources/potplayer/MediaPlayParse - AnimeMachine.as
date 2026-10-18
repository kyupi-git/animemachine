// AnimeMachine optional playback feedback, AGPL-3.0-only.
uintptr module = 0;
uintptr registerCandidate = 0;
string GetTitle() { return "AnimeMachine"; }
string GetVersion() { return "3"; }
string GetDesc() { return "AnimeMachine playback position and resume (64-bit)"; }
void OnInitialize() {
    module = HostLoadLibrary(HostGetScriptFolder() + "\\anm_playback.dll");
    if (module == 0) return;
    uintptr start = HostGetProcAddress(module, "Start");
    registerCandidate = HostGetProcAddress(module, "RegisterCandidate");
    if (start == 0 || registerCandidate == 0 || HostCallProcInt(start, "I", 0) == 0) registerCandidate = 0;
}
void OnFinalize() {
    if (module == 0) return;
    uintptr stop = HostGetProcAddress(module, "Stop");
    if (stop != 0) HostCallProcInt(stop, "I", 0);
    HostFreeLibrary(module);
    module = 0;
    registerCandidate = 0;
}
bool PlayitemCheck(const string &in path) {
    return (path.find("http://") == 0 || path.find("https://") == 0)
        && ((path.find("/api/playback/media/") > 0 && path.rfind("?anm=1") == int(path.length()) - 6)
            || (path.find("/api/playback/item/") > 0 && path.rfind("/episode.anm") == int(path.length()) - 12));
}
string PlayitemParse(const string &in path, dictionary &MetaData, array<dictionary> &QualityList) {
    string prefix = "/api/playback/media/";
    int offset = path.find(prefix);
    if (offset < 0) {
        prefix = "/api/playback/item/";
        offset = path.find(prefix);
    }
    if (offset < 0) return "";
    string base = path.substr(0, offset);
    string token = path.substr(offset + prefix.length());
    token = token.substr(0, token.find("/"));
    string target = base + "/api/playback/media/" + token + "/episode.mp4";
    string marker = "[ANM:" + token.substr(0, 12) + "]";
    JsonReader reader;
    JsonValue root;
    int resumeMs = 0;
    string manifest = base + "/api/playback/item/" + token + "/episode.anm";
    string data = HostUrlGetString(manifest, "", "", "", true);
    if (reader.parse(data, root) && root.isObject()) {
        string url = root["url"].asString();
        if (url.find(base + "/api/playback/media/" + token + "/") == 0) target = url;
        MetaData["title"] = root["title"].asString();
        resumeMs = root["resumeMs"].asInt();
    } else MetaData["title"] = marker;
    if (registerCandidate != 0) {
        string eventUrl = base + "/api/playback/started/" + token;
        if (HostCallProcInt(registerCandidate, "PPI", HostString2UIntPtr(marker), HostString2UIntPtr(eventUrl), resumeMs) != 0)
            HostUrlGetString(eventUrl, "", "Content-Type: application/json\r\n", "{\"claim\":true}", true);
    }
    return target;
}
