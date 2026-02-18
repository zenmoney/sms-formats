import http.client
import json

url = "localhost:8080"
data = {
    "sms": {
        "company_name": "СберБанк",
        "company_id": "4624",
        "sender": "900",
        "text": "Счёт карты MIR-0678 10:08 Покупка 4500р BIZNESINALOGIW Баланс: 55 333.9р"
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
