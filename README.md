Start:

1. `python3 -m venv venv`
2. `source venv/bin/activate`
3. ```
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

````
4. Первый запуск для указания юзера `python main.py`

Services
1. `sudo nano /etc/systemd/system/fastapi.service`
2. ```
[Unit]
Description=Gunicorn FastAPI Application
After=network.target

[Service]
User=root
WorkingDirectory=/root/diplom
ExecStart=/root/diplom/venv/bin/gunicorn main:app --workers 3 --worker-class uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000

[Install]
WantedBy=multi-user.target
````

3. ```
   sudo systemctl daemon-reload
   sudo systemctl start fastapi
   sudo systemctl enable fastapi
   ```

````
4. `sudo systemctl status fastapi`

Настройка Nginx
1. ```
sudo apt update
sudo apt install nginx -y
````

2.  `sudo nano /etc/nginx/sites-available/fastapi`
3.  Укажите ваш домен ```
    server {
    listen 80;
    server_name yourdomain.com wwwyourdomain.com;

        location / {
            proxy_pass http://127.0.0.1:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }

    }

```
4. `sudo ln -s /etc/nginx/sites-available/fastapi /etc/nginx/sites-enabled/`
5. `nginx -t`
6. `sudo systemctl restart nginx`
7. Установим бота `sudo apt install certbot python3-certbot-nginx -y`
8. Получим сертификат `sudo certbot --nginx -d yourdomain.com`
```
