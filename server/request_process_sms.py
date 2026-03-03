import http.client
import json

url = "localhost:8080"
data = {
    "sms": {
        "company_name": "СберБанк",
        "company_id": "4624",
        "sender": "900",
        "text": "Возврат: 100 руб из Пятерочка. Баланс 9000 руб.",
    }
}
headers = {"Content-Type": "application/json"}

conn = http.client.HTTPConnection(url)
json_data = json.dumps(data)

conn.request("POST", "/process-sms/", json_data, headers)

response = conn.getresponse()
print(response.status)
print(response.read().decode())

conn.close()
