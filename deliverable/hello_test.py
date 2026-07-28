#!/usr/bin/env python3
"""hello_test.py — smallest possible end-to-end check of the Metabrain
connection: fetch one token, send "hello", print the reply. Stdlib only.

Fill in the four UPPERCASE values, then:   python hello_test.py

Reads the failure like this:
  dies in step 1  -> token URL / client_id / secret wrong (or no network path)
  step 2 gives 404 -> BASE_URL or the API path is wrong
  step 2 gives 401 -> model name wrong, or this client not authorized for it
  CERTIFICATE_VERIFY_FAILED -> private CA: set SSL_CERT_FILE to the CA bundle
"""
import json
import urllib.parse
import urllib.request

TOKEN_URL = "https://HOST/realms/metabrain-sso/protocol/openid-connect/token"
CLIENT_ID = "YOUR_CLIENT_ID"
SECRET    = "YOUR_CLIENT_SECRET"
BASE_URL  = "https://GATEWAY-HOST/v1"      # inference endpoint, NOT the token URL
MODEL     = "MODELNAME"

# step 1: get a token
form = urllib.parse.urlencode({"grant_type": "client_credentials",
                               "client_id": CLIENT_ID,
                               "client_secret": SECRET}).encode()
tok = json.loads(urllib.request.urlopen(urllib.request.Request(
    TOKEN_URL, data=form,
    headers={"Content-Type": "application/x-www-form-urlencoded"}),
    timeout=30).read())["access_token"]
print("token ok:", tok[:25], "...")

# step 2: say hello
body = json.dumps({"model": MODEL,
                   "messages": [{"role": "user", "content": "hello"}]}).encode()
reply = json.loads(urllib.request.urlopen(urllib.request.Request(
    BASE_URL.rstrip("/") + "/chat/completions", data=body,
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + tok}),
    timeout=60).read())
print("\nmodel says:", reply["choices"][0]["message"]["content"])
