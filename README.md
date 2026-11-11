# WhatsApp 落單上載

受保護的上載入口。同事登入後可提交 WhatsApp 對話導出、圈選圖片與聯絡人截圖，並下載已完成的落單 Excel。

固定同事帳號只有這四位：

- Andy：Super Admin（超級管理員），可以為其他管理員設定密碼，也可以刪除舊提交
- Debbie、Taya、Katherine：Admin（管理員），不能刪除提交

每個人都可以在登入後更改自己的密碼。密碼不會寫進這個倉庫，也不會顯示在頁面上。

第一次登入時，如果私人 dataset 裡還沒有該帳號的雜湊，可以用托管平台的環境變數做一次性引導。登入成功後會立刻把 PBKDF2 雜湊寫入 `users/accounts.json`，之後只認這份雜湊，不再使用環境變數裡的明文。

引導用環境變數（只在該帳號還沒有雜湊時使用）：

- `SITE_USERS`：多行 `帳號:密碼`
- `SITE_USERNAME` 與 `SITE_PASSWORD`
- `SITE_PASSWORD_ANDY`、`SITE_PASSWORD_DEBBIE`、`SITE_PASSWORD_TAYA`、`SITE_PASSWORD_KATHERINE`

其他環境變數：

- `SESSION_SECRET`
- `HTTPS_ONLY`（生產環境保持 `true`）
- `HF_TOKEN` 與 `INBOX_DATASET`（私人 Hugging Face dataset，保存上載檔與密碼雜湊。免費 Web Service 的磁碟不會保留檔案）

健康檢查路徑是 `/healthz`。不要把 `.env`、密碼、token 或客人資料放進倉庫。

## WhatsApp 紀錄日期

上載時可以選擇這些紀錄屬於哪一天，或一段開始到結束的日期。日期是香港日曆日（`Asia/Hong_Kong`），可以留空。

留空時，之後的掃描會處理檔案裡找到的所有日期，manifest 不會寫入日期。有選擇時，`manifest.json` 會多兩個欄位：

- `record_date_start`：`YYYY-MM-DD`
- `record_date_end`：`YYYY-MM-DD`

只選一天時，兩個欄位都是同一天。結束日期不可早於開始日期。舊的提交沒有這兩個欄位，列表和詳情頁仍可開啟，並顯示「未指定日期」。

## 刪除舊提交

只有 Super Admin（Andy）可以刪除。近期列表和提交詳情都有刪除入口。按下後會先看到「確定刪除此提交？」，確認並通過原本的登入與 CSRF 檢查後才會刪除。Debbie、Taya、Katherine 沒有這個入口，直接呼叫也會被拒絕。

這是軟刪除，不需要新的環境變數：

- `status` 變成 `deleted`，`status_label` 是「已刪除」
- `deleted_by` 與 `deleted_at` 記下是誰、在什麼時候刪的
- 近期列表預設不再顯示
- 私人 dataset 裡的上載檔案不會在這一步清掉
- 沒有這些欄位的舊提交仍然可以開啟
