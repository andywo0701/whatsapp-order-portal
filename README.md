# WhatsApp 落單上載

受保護的上載入口。同事登入後可提交 WhatsApp 對話導出、圈選圖片、聯絡人截圖，或一份貨品清單，並下載已完成的落單 Excel。上載時不用再選這是新單還是舊單。

固定同事帳號只有這四位：

- Andy：Super Admin（超級管理員），可以為其他管理員設定密碼，也可以刪除舊提交、更正顧客名冊，以及刪除貨品清單上的一項
- Debbie、Taya、Katherine：Admin（管理員），不能刪除提交，也不能確認、更正或刪除名冊上的顧客，也不能刪除貨品。四位都可以上載貨品清單，以及查看、搜尋、下載已發布的清單

每個人都可以在登入後更改自己的密碼。密碼不會寫進這個倉庫，也不會顯示在頁面上。

第一次登入時，如果私人 dataset 裡還沒有該帳號的雜湊，可以用托管平台的環境變數做一次性引導。登入成功後會立刻把 PBKDF2 雜湊寫入 `users/accounts.json`，之後只認這份雜湊，不再使用環境變數裡的明文。

引導用環境變數（只在該帳號還沒有雜湊時使用）：

- `SITE_USERS`：多行 `帳號:密碼`
- `SITE_USERNAME` 與 `SITE_PASSWORD`
- `SITE_PASSWORD_ANDY`、`SITE_PASSWORD_DEBBIE`、`SITE_PASSWORD_TAYA`、`SITE_PASSWORD_KATHERINE`

其他環境變數：

- `SESSION_SECRET`
- `HTTPS_ONLY`（生產環境保持 `true`）
- `HF_TOKEN` 與 `INBOX_DATASET`（私人 Hugging Face dataset，保存上載檔、密碼雜湊、顧客名冊，以及貨品清單。免費 Web Service 的磁碟不會保留檔案）

可選，網站沒有它們也能啟動。之後在 Render 上設定，不要寫進 git：

- `SCAN_WEBHOOK_URL`：掃描程式的 HTTPS 網址，用來接收「立即掃描」
- `SCAN_WEBHOOK_KEY`：交給那個網址的 Bearer 金鑰

健康檢查路徑是 `/healthz`。不要把 `.env`、密碼、token、webhook 網址、webhook 金鑰或客人資料放進倉庫。

## 上載用途

上載表單預設是「自動分辨」。同事可以改選三個用途。WhatsApp 紀錄日期（不指定、單日或日期範圍）跟以前一樣，可選。

自動分辨只看檔名和副檔名，不打開檔案，也不從圖片讀取姓名、電話或價錢：

- `.zip`、`.txt`（對話導出）→ `orders`（落單資料）
- `.xlsx`、`.xls`、`.csv`、`.doc`、`.docx`，而且檔名含有貨品、商品、產品、價目、價錢、價格、`catalog`、`catalogue`、`product`、`price`、`pricelist` 或 `sku` → `catalog`（貨品清單）
- 同一筆裡只要有一個這樣的試算表或 Word，整筆就是貨品清單
- 其他檔名，包括相片，維持落單資料

同事點選的用途會蓋過自動分辨。點了「客戶資料」就一定是客戶資料，即使檔名像貨品清單。

- `orders`：落單資料。對話導出、落單圖片。沒有 `purpose` 欄位的舊提交也當作這一種。
- `customers`：客戶資料。WhatsApp 群組聯絡人截圖、聯絡人清單，或含有電話的對話。檔案只是存起來，狀態維持「待處理」，直到掃描程式處理完並發布名冊。
- `catalog`：貨品清單。Excel、Word，或貨品相片。檔案只是存起來。這個網站不會從檔案裡讀價錢；價錢要等掃描程式發布 `catalog/catalog.json`。

`manifest.json` 會寫入最後採用的 `purpose`（`orders`、`customers` 或 `catalog`），不會寫入 `auto`。近期列表和詳情頁會顯示「落單資料」、「客戶資料」或「貨品清單」。

客戶資料和貨品清單一樣遵守刪除規則：只有 Super Admin 可以軟刪除提交，確認句是「確定刪除此提交？」，檔案留在原處，預設列表不再顯示。管理員打開刪除網址會看到「沒有權限。」。

## 類型不再由同事選擇

上載表單沒有「類型」下拉，也不再預設「新單」。新提交的 `kind` 是「待判斷」。掃描程式之後自行比較上一張落單，判斷新單、已改或已取消。這個網站不會替它做這個判斷。

如果請求裡仍帶有舊表單用過的類型（新單、改單、取消、已改、已取消、聯絡人截圖、其他），會照原樣保存，方便舊的提交和測試。不認識的值會存成「待判斷」。

已經在 dataset 裡、帶有 `kind` 的舊提交會照原來的文字顯示。沒有 `kind` 的舊提交打開時顯示「待判斷」，檔案本身不會被改寫。

## 貨品清單

登入後的導覽和首頁都有「貨品清單」。四位同事都可以查看、搜尋名稱、價錢或備註，以及下載 `貨品清單.xlsx`。沒有清單時，頁面會請同事去上載貨品清單。未登入的人不能打開這些頁面。

清單欄位：

- 貨品名稱（必須是字串；沒有名稱的列會略過，不會編一個名字）
- 圖片（可選，只保存檔名。網址不會被抓取）
- 價錢（保留掃描程式看到的原文和貨幣；沒有就顯示「尚缺」）
- 備註
- 最後更新日期
- 來源提交編號

只有 Super Admin（Andy）可以刪除清單上的一項。確認句是「確定刪除此貨品？」，而且要通過原本的 CSRF。Debbie、Taya、Katherine 打開刪除網址會看到「沒有權限。」。刪除的是清單上的一列，不是把已上載的檔案清掉。

規則：

- 不會發明或換算價錢。價錢如果不是字串（例如 JSON 數字），會忽略，不會改成另一種寫法。
- 已經知道的價錢不會被空白覆蓋。
- 掃描程式沒有提到的貨品會保留。漏掉一項不會刪除它；刪除要由 Super Admin 在網站上做。
- 已被刪除的編號或同一個名稱，不會因為再次發布而回來。

## 掃描程式如何發布貨品清單

掃描程式處理 `purpose` 為 `catalog` 的提交之後，把整份清單上傳到同一個私人 dataset 的 `catalog/catalog.json`。圖片可以另外放到 `catalog/images/檔名`。dataset 還沒有 `catalog/` 時，網站會顯示空的清單，其他頁面仍可使用。

可以用 Hugging Face dataset API 上傳，或在已設定 `HF_TOKEN`（或 `HF_WRITE_TOKEN`）和 `INBOX_DATASET` 的環境執行：

```python
from pathlib import Path
from sync_inbox import publish_catalog

publish_catalog(Path("catalog.json"))
```

`publish_catalog` 會拒絕不是字串的 `name`、`price`、`image` 或 `note`，避免把數字價錢上傳出去。

`catalog/catalog.json` 例子（請只用假資料，不要把真實價錢放進倉庫）：

```json
{
  "version": 1,
  "updated_at": "2026-11-14T10:00:00+08:00",
  "updated_by": "scanner",
  "products": [
    {
      "id": "p0000abcd",
      "name": "測試蘋果",
      "image": "apple.png",
      "price": "HK$00.00",
      "note": "",
      "updated_at": "2026-11-14T10:00:00+08:00",
      "submission_ids": ["20261114-100000-abc123"]
    }
  ]
}
```

- `id`：可選，`6` 到 `64` 個英數、`_` 或 `-`。沒有的話，網站會依貨品名稱編成穩定編號，方便再次打開同一項。
- `price` 和 `name` 必須是字串。空字串表示沒有價錢，不是「尚缺」這兩個字。JSON 數字會被忽略。
- `image`：可選檔名，例如 `apple.png`。網站會在 `catalog/images/` 和來源提交的檔案裡找這個名字，不會跟任何網址要圖片。可在頁面上顯示的是 jpg、jpeg、png、webp、gif。
- `submission_ids`：相關的貨品清單提交編號。詳情頁會在找得到檔案時顯示連結。

網站讀取時會跟已經保存的清單合併，而不是整份覆蓋：

- 發布檔沒有提到的貨品會保留。
- 發布的價錢是空白時，不會清掉清單上已有的價錢。
- 新的非空白價錢會整段取代，保留掃描程式寫下的字符，不會換算。
- 已被 Super Admin 刪除的 `id` 或同一個名稱不會因為再次發布而回來。
- 讀取失敗或 JSON 無法解析時，沿用本機最後一份成功保存的清單，不會改成空的。

Super Admin 刪除一項時，會先把 `catalog/catalog.json` 和 `catalog/catalog.xlsx` 一起提交到 dataset。上傳失敗時，本機清單維持原狀。下載的 Excel 是依合併後的清單現做的，價錢儲存格用文字格式，避免試算表改寫金額。`xlsx` 裡沒有價錢的儲存格會顯示「尚缺」；JSON 裡仍是空字串。

## 立即掃描

首頁和每一筆「待處理」提交的詳情頁都有「立即掃描」。同事按下後是一次 POST，並使用原本的登入與 CSRF。

如果 `SCAN_WEBHOOK_URL` 和 `SCAN_WEBHOOK_KEY` 都有設定，而且網址是沒有帳號密碼的 `https://`，伺服器會送出：

```http
POST /你的路徑 HTTP/1.1
Host: 掃描程式的主機
Authorization: Bearer （SCAN_WEBHOOK_KEY，不會出現在頁面或紀錄）
Content-Type: application/json; charset=utf-8
Accept: application/json

{"action":"scan","source":"whatsapp-order-portal","requested_by":"Andy","submission_id":"可省略"}
```

`requested_by` 是登入的同事帳號。從某一筆待處理提交按下去時，才會帶 `submission_id`。伺服器等待最多 8 秒，不跟隨重新導向，避免把金鑰轉送到另一個網址。回應是 2xx 才算成功，頁面顯示「已發出掃描要求，做好會把 Excel 放回這裡。」

這兩個環境變數是可選的，沒有它們網站一樣可以啟動。沒有設定，或網址不是 https 時，頁面會說可以在 Grok Bot 對話裡請它掃描。暫時連絡不上時，會請同事稍後再試，或改在 Grok Bot 對話裡請它掃描。兩種情況都不會當掉，也不會把網址或金鑰寫進頁面。

每位同事每 60 秒最多按 3 次。超過會請稍候再按，而且不會再呼叫 webhook。

不要把真實的 webhook 網址或金鑰放進倉庫、測試，或頁面。Render 上之後再加這兩個變數即可，不需要新的服務。

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
