## Settings & Configuration
The project settings can be found in the [`config/settings.json`](https://github.com/KathiraveluLab/Diomede/blob/main/config/settings.json) file.  
Modify this file to configure different aspects of the project. 
# Diomede  

Diomede is an open-source DICOM telemedicine toolkit for organizing, querying, and sharing DICOM images efficiently.  

---

## ⚙️ Settings & Configuration  
Diomede requires some configurations to work correctly. Below are the key settings and where to find them.  

### **🔹 Where to Find Project Settings**  
The main configuration files are:  
- **[config/settings.json](https://github.com/KathiraveluLab/Diomede/blob/main/config/settings.json)** → Stores app configurations.  
- **[.env](https://github.com/KathiraveluLab/Diomede/blob/main/.env.example)** → Stores environment variables.  

### **🔹 Configuring the Project**  
Before running the project, you may need to adjust the settings.  

#### **1️⃣ Environment Variables (`.env`)**  
Rename the `.env.example` file and update values as needed:  
```sh
cp .env.example .env
nano .env  # Modify values

### Example Configuration:
```json
{
  "database_url": "mongodb://localhost:27017",
  "debug_mode": true
}
---

After updating the settings, start the application using:

npm start  # If using Node.js  
python app.py  # If using Python  

❓ Troubleshooting
Database Connection Issues?

Ensure MongoDB is running (sudo systemctl start mongod).
Check if DATABASE_URL is correct in settings.json.
Port Already in Use?
Change PORT in .env or settings.json.