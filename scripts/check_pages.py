"""HEAD every procedure's mitos.gov.gr page, sequentially, one second apart. Resumes: skips ids already in the output."""

import json
import os
import sys
import time
import urllib.parse

import httpx

facts_path, out_path = sys.argv[1], sys.argv[2]
done = set()
if os.path.exists(out_path):
    done = {json.loads(line)["procedure_id"] for line in open(out_path, encoding="utf-8")}
rows = [json.loads(line) for line in open(facts_path, encoding="utf-8")]
with httpx.Client(timeout=30, follow_redirects=False, headers={"User-Agent": "mitos-rag link check"}) as client, open(
    out_path, "a", encoding="utf-8"
) as out:
    for row in rows:
        if row["procedure_id"] in done:
            continue
        url = "https://mitos.gov.gr/index.php/" + urllib.parse.quote("ΔΔ:" + row["title"].replace(" ", "_"))
        try:
            status = client.head(url).status_code
        except httpx.HTTPError as exc:
            status = f"error:{type(exc).__name__}"
        out.write(json.dumps({"procedure_id": row["procedure_id"], "status": status}) + "\n")
        out.flush()
        time.sleep(1.0)
print("done", len(done), "previously")
