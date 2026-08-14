import urllib.request, urllib.error, json

out = []
try:
    r = urllib.request.urlopen("http://127.0.0.1:8081/api/data", timeout=15)
    d = json.loads(r.read().decode())
    out.append("api/data OK bars=%d" % len(d.get("klines", [])))
except urllib.error.HTTPError as e:
    body = e.read().decode(errors="replace")
    out.append("HTTP %d body: %s" % (e.code, body[:3000]))
except Exception as e:
    out.append("ERR %s" % e)

with open(r"health_check.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("ok")
