#!/bin/bash
set -e

echo "=== tmux-discord セットアップ ==="
echo ""
read -rp "Discord Bot Token を貼り付けてください: " token

cat > /home/w00dst0ck/apps/tmux-discord/.env <<EOF
DISCORD_TOKEN=${token}
DISCORD_CHANNEL_ID=
TMUX_SESSION=0
EOF

echo ""
echo "起動中..."
cd /home/w00dst0ck/apps/tmux-discord
pm2 start ecosystem.config.js
pm2 save

echo ""
echo "完了！DiscordでBotが入っているチャンネルで !setchannel と打てばそのチャンネルに固定されます。"
