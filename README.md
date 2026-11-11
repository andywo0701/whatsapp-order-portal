# WhatsApp 落單上載

受保護的上載入口：已登入的指定人員可提交 WhatsApp 對話導出、圈選圖片與聯絡人截圖，並下載已完成的落單 Excel。

登入帳號、Session 密鑰與儲存用 token 只放在托管平台的環境變數，不要寫進這個倉庫。

主要環境變數：

- `SITE_USERNAME` / `SITE_PASSWORD`（或 `SITE_USERS`）
- `SESSION_SECRET`
- `HTTPS_ONLY`（生產環境保持 `true`）
- `HF_TOKEN` 與 `INBOX_DATASET`（用私人 Hugging Face dataset 保存上載檔，因為免費 Web Service 沒有持久磁碟）
