#!/usr/bin/python3
import json
import dashboard

status, document = dashboard.respond()
print('Status: ' + status + '\r\nContent-Type: application/json; charset=utf-8\r\n'
      'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n' +
      json.dumps(document, ensure_ascii=False))
