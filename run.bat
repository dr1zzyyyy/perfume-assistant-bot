cat << 'EOF' > run.bat
@echo off
chcp 65001 > nul

if not exist venv (
    echo [INFO] Создаем виртуальное окружение venv...
    python -m venv venv
)

call venv\Scripts\activate

echo [INFO] Проверяем зависимости...
python -m pip install --upgrade pip > nul
pip install -r requirements.txt

if not exist .env (
    echo [WARNING] Файл .env не найден! Копирую .env.example...
    copy .env.example .env
    echo [ACTION] Открой .env, укажи свои токены и запусти run.bat снова.
    pause
    exit /b 1
)

echo [INFO] Запуск бота...
python bot.py
pause
EOF
