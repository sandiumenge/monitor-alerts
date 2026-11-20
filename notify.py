"""Windows toast + sound, no dependencies (same approach as room-scraper/roomscraper/notify.py).

The toast is raised through PowerShell with PowerShell's own AppUserModelID, so nothing has to be installed.
Clicking it opens the listing URL. Optional phone push through ntfy.sh when `ntfy_topic` is set in alerts.json.
Every function swallows its own errors: a failed notification must never break a run.
"""
import base64
import json
import shutil
import subprocess
import urllib.request

_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
_TOAST = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null
$xml = @"
<toast activationType="protocol" launch="LAUNCH_URL" scenario="reminder">
  <visual><binding template="ToastGeneric"><text>TITLE</text><text>BODY</text></binding></visual>
  <actions><action content="Abrir anuncio" activationType="protocol" arguments="LAUNCH_URL"/></actions>
</toast>
"@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("APP_ID").Show((New-Object Windows.UI.Notifications.ToastNotification $doc))
"""


def _esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def sound():
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
        return True
    except Exception:  # noqa: BLE001
        return False


def toast(title, body, url=""):
    try:
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            return False
        script = (_TOAST.replace("TITLE", _esc(title)).replace("BODY", _esc(body))
                  .replace("LAUNCH_URL", _esc(url or "https://www.marktplaats.nl")).replace("APP_ID", _APP_ID))
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                           capture_output=True, timeout=20, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def ntfy(topic, title, body, url="", priority=4):
    """Phone push via ntfy.sh (free app). Only listing data is sent, never personal data."""
    if not topic:
        return False
    try:
        msg = {"topic": topic, "title": title, "message": body, "tags": ["desktop_computer"], "priority": priority}
        if url:
            msg["click"] = url
            msg["actions"] = [{"action": "view", "label": "Abrir anuncio", "url": url}]
        req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(msg).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False
