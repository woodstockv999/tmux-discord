module.exports = {
  apps: [
    {
      name: "tmux-discord",
      script: "python3",
      args: "bot.py",
      cwd: "/home/w00dst0ck/apps/tmux-discord",
      restart_delay: 5000,
      max_restarts: 10,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
