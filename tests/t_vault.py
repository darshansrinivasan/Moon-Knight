"""Protected settings must survive an empty save — the vertex_project bug."""
import db
import vault

db.init_db()
fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


print("=== the exact production regression ===")
vault.set_settings({"vertex_project": "sd-vertexai-studio",
                    "vertex_models": "gemini-2.5-flash"}, "admin")
check("saved", vault.get_setting("vertex_project"), "sd-vertexai-studio")

# The Admin page opens, the project dropdown fails to load, admin clicks Save.
refused = vault.set_settings(
    {"vertex_project": "", "vertex_quota_project": "",
     "vertex_location": "us-central1", "vertex_models": "gemini-2.5-flash"},
    "admin",
)
check("vertex_project refused", "vertex_project" in refused, True)
check("value survived", vault.get_setting("vertex_project"), "sd-vertexai-studio")
check("source still admin", vault.setting_source("vertex_project"), "admin")

print()
print("=== an explicit clear is still possible ===")
refused = vault.set_settings({"vertex_project": ""}, "admin", allow_clear=True)
check("not refused with allow_clear", refused, [])
check("now cleared", vault.get_setting("vertex_project"), "")

print()
print("=== an empty protected setting with no stored value is fine ===")
refused = vault.set_settings({"vertex_project": ""}, "admin")
check("no refusal when nothing to lose", refused, [])

print()
print("=== unprotected settings still clear normally ===")
vault.set_settings({"slack_channel": "C123"}, "admin")
vault.set_settings({"slack_channel": ""}, "admin")
check("slack_channel cleared", vault.get_setting("slack_channel"), "")

print()
print("=== schedule fields are protected too (the runs.html bug) ===")
vault.set_settings({"schedule_time": "09:30", "schedule_tz": "Asia/Kolkata"}, "admin")
refused = vault.set_settings({"schedule_time": "", "schedule_tz": ""}, "admin")
check("both refused", sorted(refused), ["schedule_time", "schedule_tz"])
check("time survived", vault.get_setting("schedule_time"), "09:30")
check("tz survived", vault.get_setting("schedule_tz"), "Asia/Kolkata")

print()
print("=== a real update still applies ===")
vault.set_settings({"schedule_time": "07:15"}, "admin")
check("updated", vault.get_setting("schedule_time"), "07:15")

print()
if fails:
    print(f"FAILURES ({len(fails)}): {fails}")
    raise SystemExit(1)
print("ALL VAULT ASSERTIONS PASSED")
