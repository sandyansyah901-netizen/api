.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt


mysql -u root -p -e "CREATE DATABASE manga_reader"

python -c "import secrets; print(secrets.token_urlsafe(32))"

pip install pillow python-magic aiofiles filetype

python seed_data.py

uvicorn main:app --reload

mysqldump -u root -p manga_reader > manga_reader_backup.sql

iconv -f utf-16 -t utf-8 manga_reader_backup.sql -o fixed.sql

mysqldump --default-character-set=utf8mb4 -u root -p manga_reader > manga_reader_backup.sql

Get-ChildItem Env: | Where-Object { $_.Name -like "*RCLONE*" }