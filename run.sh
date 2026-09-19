cat << 'EOF' > run.sh
#!/usr/bin/env bash
set -e

if [ ! -d "venv" ]; then
    echo "⚙️ Создаем venv..."
    python3 -m venv venv
fi

source venv/bin/activate

echo "📦 Устанавливаем зависимости..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

if [ ! -f ".env" ]; then
    echo "⚠️ Файл .env не найден! Создаю копию из .env.example..."
    cp .env.example .env
    echo "👉 Заполни .env своими ключами и запусти скрипт снова."
    exit 1
fi

echo "🚀 Запуск бота..."
python bot.py
EOF
