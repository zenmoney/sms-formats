import http.client
import json

url = "localhost:8080"
data = {
    "sms": {
        "company_name": "СберБанк",
        "company_id": "4624",
        "sender": "900",
        "text": "930 ₽ — Осталось: 6 295,21 ₽ Счёт карты МИР •• 0632 ║ Всё ОК! Оплата в Самокат"
    }
}
headers = {
    "Content-Type": "application/json"
}

conn = http.client.HTTPConnection(url)
json_data = json.dumps(data)

conn.request("POST", "/process-sms/", json_data, headers)

response = conn.getresponse()
print(response.status)
print(response.read().decode())

conn.close()
