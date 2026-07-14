module.exports = {
  apps: [
    {
      name: "dashboard",
      script: "dashboard_backend.py",
      interpreter: "python3",
      env: {
        // 🛠️ Step 1 එකේ generate කරගත්ත token එකම මෙතනට දාන්න
        DASHBOARD_TOKEN: "N5QxuF9LIGJ9S4R0k8iWF8OTdh3ST-W5"
      }
    }
  ]
};
