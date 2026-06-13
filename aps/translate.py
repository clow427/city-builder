import time
import requests
from config import APS_HOST


def start_translation(token, urn, root_filename=None):
    inp = {"urn": urn}
    if root_filename:                      # zip input (e.g. OBJ + MTL)
        inp["compressedUrn"] = True
        inp["rootFilename"] = root_filename
    r = requests.post(
        f"{APS_HOST}/modelderivative/v2/designdata/job",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "x-ads-force": "true"},
        json={"input": inp,
              "output": {"formats": [{"type": "svf2", "views": ["2d", "3d"]}]}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def wait_until_done(token, urn, timeout_s=600, interval_s=10):
    url = f"{APS_HOST}/modelderivative/v2/designdata/{urn}/manifest"
    waited = 0
    while waited < timeout_s:
        m = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
        status = m.get("status")
        print("translate status:", status, m.get("progress"))
        if status == "success":
            return m
        if status == "failed":
            raise RuntimeError(f"translation failed: {m}")
        time.sleep(interval_s)
        waited += interval_s
    raise TimeoutError("translation did not finish in time")
