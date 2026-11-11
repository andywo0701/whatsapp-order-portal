# WhatsApp 落單上載

受保護的上載入口。同事登入後可提交 WhatsApp 對話導出、圈選圖片與聯絡人截圖，並下載已完成的落單 Excel。同一頁也可以改選「客戶資料」，上載用來填寫顧客名冊的聯絡人截圖或含電話的對話。

固定同事帳號只有這四位：

- Andy：Super Admin（超級管理員），可以為其他管理員設定密碼，也可以刪除舊提交和更正顧客名冊
- Debbie、Taya、Katherine：Admin（管理員），不能刪除提交，也不能確認、更正或刪除名冊上的顧客

每個人都可以在登入後更改自己的密碼。密碼不會寫進這個倉庫，也不會顯示在頁面上。

第一次登入時，如果私人 dataset 裡還沒有該帳號的雜湊，可以用托管平台的環境變數做一次性引導。登入成功後會立刻把 PBKDF2 雜湊寫入 `users/accounts.json`，之後只認這份雜湊，不再使用環境變數裡的明文。

引導用環境變數（只在該帳號還沒有雜湊時使用）：

- `SITE_USERS`：多行 `帳號:密碼`
- `SITE_USERNAME` 與 `SITE_PASSWORD`
- `SITE_PASSWORD_ANDY`、`SITE_PASSWORD_DEBBIE`、`SITE_PASSWORD_TAYA`、`SITE_PASSWORD_KATHERINE`

其他環境變數：

- `SESSION_SECRET`
- `HTTPS_ONLY`（生產環境保持 `true`）
- `HF_TOKEN` 與 `INBOX_DATASET`（私人 Hugging Face dataset，保存上載檔、密碼雜湊，以及顧客名冊。免費 Web Service 的磁碟不會保留檔案）

健康檢查路徑是 `/healthz`。不要把 `.env`、密碼、token 或客人資料放進倉庫。

## 上載用途

上載表單先選用途，預設是落單資料。兩種用途都可以再用原本的 WhatsApp 紀錄日期（不指定、單日或日期範圍）。

- `orders`：落單資料。對話導出、落單圖片，和以前一樣。沒有 `purpose` 欄位的舊提交也當作這一種。
- `customers`：客戶資料。WhatsApp 群組聯絡人截圖、聯絡人清單，或含有電話的對話。檔案只是存起來，狀態維持「待處理」，直到掃描程式處理完並發布名冊。

`manifest.json` 會寫入 `purpose`。近期列表和詳情頁會顯示「落單資料」或「客戶資料」。這個網站不會從圖片讀取或猜測姓名、電話。

客戶資料一樣遵守刪除規則：只有 Super Admin 可以軟刪除，確認句是「確定刪除此提交？」，檔案留在原處，預設列表不再顯示。

## 顧客名冊

登入後的導覽和首頁都有「顧客名冊」。四位同事都可以查看、搜尋姓名或電話、用「全部 / 尚缺電話 / 待確認」篩選，以及下載 `顧客名冊.xlsx`。沒有名冊時，頁面會請同事去上載客戶資料。未登入的人不能打開這些頁面。

名冊欄位：

- 顧客姓名
- 電話（保留發布時的寫法；沒有電話就顯示「尚缺」）
- 號碼來源：對話、聯絡人截圖、尚缺。超級管理員在本網站更正姓名或電話之後，來源會改成「人手輸入」，表示這是人手更正，不是從圖片讀取的
- 最後更新日期
- 狀態：正常或待確認
- 備註／待確認

只有 Super Admin（Andy）可以確認待確認的顧客、更正姓名或電話，或刪除名冊上的一位顧客。確認句分別是「確定確認此顧客？」、「確定儲存此更正？」和「確定刪除此顧客？」，而且要通過原本的 CSRF。更正時必須填說明。Debbie、Taya、Katherine 打開這些網址會看到「沒有權限。」。每次確認、更正、刪除都會記下是誰、在什麼時候、改了什麼。

規則：

- 不會發明姓名或電話。電話如果不是字串（例如 JSON 數字），會忽略，不會改成另一種寫法。
- 已經知道的電話不會被空白覆蓋。
- 同一個姓名出現兩個不同電話時，狀態維持待確認，頁面會同時列出兩個號碼，不會自動選一個。

## 掃描程式如何發布名冊

掃描程式處理 `purpose` 為 `customers` 的提交之後，把整份名冊上傳到同一個私人 dataset 的 `customers/roster.json`。dataset 還沒有 `customers/` 時，網站會顯示空的名冊，其他頁面仍可使用。

可以用 Hugging Face dataset API 上傳，或在已設定 `HF_TOKEN`（或 `HF_WRITE_TOKEN`）和 `INBOX_DATASET` 的環境執行：

```python
from pathlib import Path
from sync_inbox import publish_roster

publish_roster(Path("roster.json"))
```

`customers/roster.json` 例子（請只用假資料，不要把真實電話放進倉庫）：

```json
{
  "version": 1,
  "updated_at": "2026-11-11T18:00:00+08:00",
  "updated_by": "scanner",
  "customers": [
    {
      "id": "c0000abcd",
      "name": "測試顧客甲",
      "phone": "0000-0000",
      "phone_source": "contact_screenshot",
      "status": "pending",
      "note": "",
      "submission_ids": ["20261111-120000-abc123"]
    }
  ]
}
```

- `id`：可選，`6` 到 `64` 個英數、`_` 或 `-`。沒有的話，網站會依姓名併到現有顧客，或自己編一個。
- `phone_source`：`conversation`（對話）、`contact_screenshot`（聯絡人截圖）、`manual`（人手輸入）、或留空（尚缺）。也可以直接寫中文。掃描程式不應把新讀到的號碼標成人手輸入。
- `status`：`ok`（正常）或 `pending`（待確認）。同一個姓名有多於一個電話時，網站會改成待確認。
- `submission_ids`：相關的客戶資料提交編號。詳情頁會在找得到檔案時顯示連結。
- `phone` 和 `name` 必須是字串。空字串表示沒有，不是「尚缺」這兩個字。

網站讀取時會跟已經保存的名冊合併，而不是整份覆蓋：

- 發布檔沒有提到的顧客會保留。漏掉一個人不會刪除他；刪除要由 Super Admin 在網站上做。
- 發布的電話是空白時，不會清掉名冊上已有的電話。
- 同一個姓名如果同時有兩個不同的非空白電話，兩個都保留，狀態是待確認，不會自動挑選。
- 已被 Super Admin 刪除的 `id` 不會因為再次發布而回來。同一個姓名加上同一組電話也不會回來。
- 讀取失敗或 JSON 無法解析時，沿用本機最後一份成功保存的名冊，不會改成空的。

Super Admin 的確認、更正和刪除會先把 `customers/roster.json` 和 `customers/roster.xlsx` 一起提交到 dataset。上傳失敗時，本機名冊維持原狀。下載的 Excel 是依合併後的名冊現做的，電話儲存格用文字格式，避免試算表改寫號碼。`xlsx` 裡沒有電話的儲存格會顯示「尚缺」；JSON 裡仍是空字串。

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
