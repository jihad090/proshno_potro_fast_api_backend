pip install -r requirements.txt
<!-- uvicorn main:app --reload --port 8000 -->
uvicorn main:app --reload --port 8000 --host 0.0.0.0

to find mac ip
ipconfig getifaddr en0    (my mac: 192.168.0.100)# proshno_potro_fast_api_backend
