# Invoice Upload System (InvUpload)

**GitHub:** https://github.com/operations236/GasSnap-uploadapp (private)  
**Live app root:** `/opt/gassnaptools/upload-app`  
**Public:** https://upload.gassnap.io → uvicorn :8010 (`gassnap-upload.service`)

## Docs
- `VISION.md` — product goals and principles
- `CONTEXT.md` — current status and working rules
- `VENDORS.md` — how to add a vendor
- `VALIDATION.md` — production-ready anchors
- `ITEM_PACK_MASTER.md` — UPC → units/case master (DayClose tab)
- `builder-agent.md` / `reviewer-agent.md` — agent prompts

## Never commit (gitignored)
`.env`, `pins.json`, `google-credentials.json`, `store_sheets.json`, `venv/`, `uploads/`, `logs/`

## Local secrets (prod already has these)
```bash
# after clone
cp store_sheets.example.json store_sheets.json   # then edit sheet IDs
# pins.json — PIN→store map (mode 600)
# google-credentials.json — Sheets SA (or GOOGLE_CREDENTIALS=)
# .env — GEMINI_API_KEY etc. (or load from /opt/gassnap/.env)
```

## Quick start (dev)
```bash
cd /opt/gassnaptools/upload-app
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn main:app --host 127.0.0.1 --port 8010
```

## Prod restart
```bash
sudo systemctl restart gassnap-upload && sleep 1 && systemctl show gassnap-upload -p MainPID --value && curl -sS http://127.0.0.1:8010/health | python3 -c "import sys,json;o=json.load(sys.stdin)['ocr'];print(o.get('qa_foot_tolerance'),o.get('qa_review_rate'),len(o.get('vendors')or[]))"
```

## Item Pack Master upsert
```bash
./venv/bin/python scripts/upsert_item_pack_master.py
./venv/bin/python scripts/upsert_item_pack_master.py --tabs "Inv - Killbuck" --dry-run
```

## Add a vendor
1. Real invoice via upload.gassnap.io
2. One `VendorSpec` in `vendors.py` (+ `critical_rules`)
3. Restart uvicorn / systemd unit
4. Checklist in `VENDORS.md` + anchors in `VALIDATION.md`
