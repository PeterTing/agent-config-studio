# 使用教學

這份文件說明這個工具在做什麼、每個畫面在回答什麼問題，以及它被設計來搭配的三種例行動作。
只讀一節的話，讀[看懂結論](#看懂結論)就好，其餘是細節。

> 英文版：[TUTORIAL.md](TUTORIAL.md)。兩份內容相同，改動時請一起改。

## 目錄

- [第一次執行](#第一次執行)
- [看懂結論](#看懂結論)
- [六個分頁](#六個分頁)
- [怎麼讀那張圖](#怎麼讀那張圖)
- [三種例行動作](#三種例行動作)
- [五種操作](#五種操作)
- [按下去之前：安全模型](#按下去之前安全模型)
- [兩個 runtime，一份來源](#兩個-runtime一份來源)
- [讓規則跟上規範演進](#讓規則跟上規範演進)
- [定期健檢](#定期健檢)
- [疑難排解](#疑難排解)
- [指令速查](#指令速查)

---

## 第一次執行

```bash
git clone <repo> && cd agent-config-studio
python3 -m studio.cli health
```

不用安裝。Python 3.11+，只用標準函式庫。

第一次執行會掃描 `~/.claude` 與 `~/.codex`、建立本機歷史索引、跑 56 條檢查，然後印出結論。
報告寫進 `var/reports/`，除此之外什麼都不動 —— 掃描與評分永遠不會修改你的設定。

第一次大約要一分鐘：它會讀過你所有的對話紀錄，用來判斷你實際上在用什麼。
結果會按檔案快取，第二次之後大約一秒。

接著開儀表板：

```bash
python3 -m studio.cli serve
```

## 看懂結論

儀表板最上面只講一句話，因為日常真正要回答的只有一個問題：**我需要做什麼嗎？**

> ✓ **健康 —— 沒有需要你處理的項目**

或

> ✕ **不健康 —— N 項需要你處理**

畫面上其他東西都是這句話的佐證。其中有三個數字經常看起來嚇人，但其實不是問題：

| 數字 | 意思 |
| --- | --- |
| **vendor** | 出現在 plugin 或 toolkit 提供的檔案裡。改了下次升級就被蓋掉，所以永遠不計入結論。要處理就升級、移除，或記 waiver。 |
| **minor** | 不影響行為的改善項：沒在用但還開著的 plugin、殘留的備份檔、長的參考檔沒有目錄。 |
| **waived** | 你寫了理由、決定不修的項目。waiver 是留下紀錄的決定，不是靜音鍵。 |

真正決定結論的只有 **blocking**：出現在你自己擁有的檔案裡、important 或 critical、而且沒有 waive 的項目。

刻意沒有 0–100 分數。一個總分只會讓人去調分數，而不是去調設定。

## 六個分頁

每個分頁回答一個不同的問題。

**Overview** —— *現在健康嗎？在變好還是變差？* 結論、預載 metadata 的成本、可更新項目、
指令檔行數，以及每次歷史執行一根長條。趨勢全綠代表一直維持健康。

**Graph** —— *這些東西怎麼串起來的？* 見[怎麼讀那張圖](#怎麼讀那張圖)。

**Findings** —— *到底哪裡有問題、我要做什麼？* 每一項的嚴重度、歸屬、位置、問題描述、修法，
以及規則所依據的文件連結。可依嚴重度、歸屬、分類或關鍵字過濾。
能自動修的會出現按鈕；不能自動修的會說明**為什麼**不能。

**Plugins & updates** —— *有沒有東西過期了？* 每個 plugin 與 toolkit 的本機版本對遠端版本，
各自附更新按鈕。無法比對的一律標成 **unknown**，絕不謊報為最新。

**清單** —— *我有哪些東西可以用、怎麼用？* 這是一份目錄：每個 skill / command / agent /
workflow 一張卡，寫著它**做什麼**、**什麼時候會被觸發**、**你怎麼叫它**，以及它在哪個檔案。
上方「怎麼取用」會先說明這一類東西的觸發機制（skill 自動、command 要打斜線、hook 是事件觸發）。
預設只列**可取用的** —— 載入不到的（不在 agent 會讀的目錄裡）要切「全部」才看得到，
而且會明確標示叫了也沒反應。

**規範與排程** —— *規範本身有沒有變？每日健檢在跑嗎？* 重新抓取每份被引用的文件跟基準比對，可選用 AI 分析變動對規則的影響。偵測是決定性的；分析只是意見，永遠不會自己改規則。

**Sync** —— *產生出來的指令檔有沒有被手改過？* `CLAUDE.md` 與 `AGENTS.md` 是否還與 canonical
來源一致、待套用的 diff，以及所有還原點。

## 怎麼讀那張圖

先認形狀：

| | |
| --- | --- |
| **顏色** | 種類：instruction、skill、workflow、command、agent、hook、plugin、canonical 來源 |
| **大小** | 份量 —— skill 的內文長度、plugin 的 skill 數量 |
| **紅圈** | 這個檔案有 blocking 項目 |
| **線的樣式** | 關係：引用、呼叫、宣告的鏡像、由誰產生、未宣告的重複、名稱衝突 |

會互相蓋到的標籤會被藏起來，放大後才出現，所以狀態列會寫類似 `58/265 labels`。
這是設計如此，不是壞掉。

**不要一開始就勾「展開 plugin skills」。** 那會讓畫面從幾百個節點變成一千多個，
把你自己寫的設定埋掉。可行的順序是：

1. 預設畫面已經幫你勾好 **只看有連線的**，所以一開始看到的就是真正的骨架，
   不是一團孤立的點
2. 用 kind 過濾成 `instruction` 或 `workflow` —— 看路由結構
3. **點一個節點** —— 側欄會列出它連到的所有東西，以及該檔案的問題
4. 要找特定東西時才展開 plugin skills，並搭配搜尋框
5. 看不懂線的樣式就看圖例 —— 每一種線都畫了實際樣本並附一句白話解釋

這張圖最適合回答具體問題 —— *誰在用這個 skill？這兩個為什麼一模一樣？為什麼沒有東西指向這個
workflow？* —— 而不是拿來盯著整張圖看。

## 三種例行動作

**每天，或任何你想確認的時候。** 開儀表板，看結論那一行。就這樣。綠的就關掉。

**新增或修改 skill 之後。**

```bash
python3 -m studio.cli health
```

有 blocking 就以 1 結束，所以要塞進 pre-commit hook 或 CI 都可以。

**每個月，或覺得東西開始臃腫的時候。**

```bash
python3 -m studio.cli usage      # 我實際上用了什麼
python3 -m studio.cli fix --list # 有什麼可以自動清掉
python3 -m studio.cli update     # 有什麼過期了
python3 -m studio.cli specs      # 規範本身有沒有變
```

## 五種操作

每一種都會先預覽、備份被取代的內容，並且都能用 `studio rollback` 還原。

### 1. 健檢

```bash
python3 -m studio.cli health
python3 -m studio.cli health --with-updates   # 同時查遠端
python3 -m studio.cli health --json           # 機器可讀
```

儀表板：右上角 **Run check**。

### 2. 修復

```bash
python3 -m studio.cli fix --list    # 哪些可自動修，其餘為何不行
python3 -m studio.cli fix           # 預覽 diff
python3 -m studio.cli fix --apply   # 實際寫入
```

儀表板：每個 finding 一個按鈕，另有適合批次處理的 **fix everything**。

只有在修法是機械性的 —— 只有一種合理結果、不需要判斷意圖 —— 才會提供自動修。
幫過長的參考檔加目錄算；把過大的 skill 拆開不算。

有兩條界線值得知道：

- **vendor 的項目永遠沒有按鈕。** 寫進一個 plugin 下次會蓋掉的檔案是浪費時間。
- **個別判斷維持個別。** 回報沒在用的 plugin 那條規則，會標出所有沒有使用紀錄的 plugin，
  但分類器會刻意保留其中一些 —— 一個 plugin 可以零呼叫紀錄，卻是你的指令依賴的對象。
  這類項目各自一個按鈕，而且不納入 *fix everything*。

### 3. 整合（會用到模型）

```bash
python3 -m studio.cli consolidate                  # 提案、驗證、顯示 diff
python3 -m studio.cli consolidate --apply          # 寫入通過驗證的方案
python3 -m studio.cli consolidate --only SK007 --limit 3
```

用在沒有單一正確答案的項目：過大的 skill 該把哪幾節移出去、兩個一模一樣的檔案是刻意的鏡像
還是殘留。

模型只提方案，永遠不碰檔案。接著由程式對照它宣稱要處理的那個檔案逐項檢查 ——
這些節真的存在嗎、有沒有同一節被宣告兩次、結果有沒有滿足規則、目標路徑有沒有留在 skill 內、
有沒有內容遺失 —— 任何一項不過就整案退回，不會部分套用。

每項大約 $0.2–0.3。指令會在 `--apply` 寫入任何東西之前先印出總額。

### 4. 更新

```bash
python3 -m studio.cli update                    # 有什麼過期、會怎麼更新
python3 -m studio.cli update --apply
python3 -m studio.cli update --only gstack --apply
```

儀表板：每一項旁邊的 **update** 按鈕。

更新一律呼叫該套件自己的更新程式。plugin 走 `claude plugin update`；
git checkout 的 toolkit 走它自己文件寫的 stash → fetch → reset → `setup` 流程，
外加它附的版本遷移腳本。升級前的 commit 會先記下來，所以 setup 失敗時還有地方可以回去。

plugin 更新後要重開 Claude Code 才會生效。

### 5. 同步

```bash
python3 -m studio.cli sync           # 預覽
python3 -m studio.cli sync --apply   # 寫入
```

把 canonical 來源渲染成 `CLAUDE.md` 與 `AGENTS.md`，並重新同步所有宣告過的鏡像。
見[兩個 runtime，一份來源](#兩個-runtime一份來源)。

### 還原任何操作

```bash
python3 -m studio.cli backups          # 所有還原點
python3 -m studio.cli rollback <id>    # 把那些位元組原樣放回去
```

## 按下去之前：安全模型

四個性質，依重要性排列：

**掃描與評分永遠不寫入。** 所有寫入都先經過 change set，先存下要被取代的原始位元組。

**儀表板預設唯讀。** 只有用 `--allow-actions` 啟動時才存在寫入端點。
即使開了，也要求 `Origin` 相符與每個 process 一組的 session token，
所以另一個分頁的網頁沒辦法驅動它。沒開這個旗標時按鈕是停用的，並改為顯示對應的指令。

**啟發式規則不能動手。** 56 條規則裡，有些是精確的（檔案大小、雜湊、版本號），
有些是判斷型的樣式比對（這句話有沒有加上一個驗證步驟？這兩節是不是在講同一件事？）。
判斷型的有時候會錯。它們**沒有任何一條**有自動修 —— 只回報，由人決定。

**AI 提案，程式定案。** 模型只在兩個地方被呼叫：提出整合方案，以及讀一份變動過的規範。
兩者都只產生提案、都由程式驗證後才動作，而且都不會編輯規則或寫入檔案。

## 兩個 runtime，一份來源

Claude Code 讀 `CLAUDE.md`，Codex 讀 `AGENTS.md`。手動維護兩份同樣的規則，
保證會走鐘。改成兩份都用渲染的：

```
canonical/core.md          共用規則
canonical/claude.delta.md  Claude 專屬的工具名稱與流程閘
canonical/codex.delta.md   Codex 專屬的工具名稱與流程閘
        │  studio sync
        ├─→ ~/.claude/CLAUDE.md
        └─→ ~/.codex/AGENTS.md
```

設定方式：

```bash
cp canonical/examples/core.example.md          canonical/core.md
cp canonical/examples/claude.delta.example.md  canonical/claude.delta.md
cp canonical/examples/codex.delta.example.md   canonical/codex.delta.md
cp canonical/examples/governance.example.json  canonical/governance.json
# 編輯後：
python3 -m studio.cli sync --apply
```

改來源，不要改渲染出來的檔案。改渲染出來的檔案就是 drift，`MR003` 會失敗並指出第一行差異。

`canonical/governance.json` 另外宣告**鏡像**（必須位元組相同的路徑）、
**vendored** 路徑（不歸你修），以及 **waivers**（附理由、已知悉的項目）。

這一整套都是選用的。不用它，工具照樣掃描、評分、畫圖、查更新，只是沒有渲染出來的指令檔
與 drift 偵測。

## 讓規則跟上規範演進

每條規則都引用一份公開文件。那些文件會改版，而依據去年建議寫成的規則會變成理直氣壯的錯，
不會有任何東西注意到。

```bash
python3 -m studio.cli specs             # 引用的文件有變動嗎
python3 -m studio.cli specs --review    # 問：變了什麼、相關規則還成立嗎
python3 -m studio.cli specs --accept    # 把目前內容記為新基準
```

偵測是決定性的：正規化後的雜湊，不用模型，不做判斷。重排一個段落不會被當成規範變動。

`--review` 是唯一會呼叫模型的部分，而且它只產生審視結果。
**規則程式碼永遠不會被自動修改。** 一條規則是一個關於「規範說了什麼」的主張，
這個主張只應該在人同意時才改變。`--accept` 代表你看過了。

## 定期健檢

```bash
scripts/install-launchd.sh install   # 每天 09:20 —— 跑規則、查遠端更新、
                                     # 並重新抓取每份被引用的規範
scripts/install-launchd.sh status    # 有載入嗎？跟 repo 有沒有落差？上次結論？
scripts/install-launchd.sh run-now   # 立刻跑一次並等結果
```

目前只支援 macOS。

排程刻意從 `~/Library/Application Support` 底下的套件副本執行，而不是從 repo：
macOS TCC 會擋掉 LaunchAgent 讀 `~/Documents`，而把 `WorkingDirectory` 指到那裡，
會讓 Python 在 `getcwd()` 就卡住，連你的程式都還沒開始跑。
共用狀態放在副本旁邊，repo 的 `var/reports` 是指向它的 symlink，所以儀表板和排程共用一份歷史。
`status` 會回報 repo 與副本之間的落差 —— 改過 `studio/` 或 `canonical/` 後要重跑 `install`。

## 疑難排解

**圖是一團沒有標籤的點。** 你在太遠的縮放看太多節點。確認「只看有連線的」有勾（預設就勾），
取消勾選「展開 plugin skills」，或放大 —— 標籤會隨空間出現。

**按鈕是灰的。** 儀表板是唯讀模式。改用 `python3 -m studio.cli serve --allow-actions` 重啟。

**`health` 很慢。** 第一次會讀完整個對話歷史。之後有按檔案的快取，大約一秒。
如果每次都慢，代表快取沒寫進去 —— 檢查 `var/` 是否可寫。

**某個 finding 看起來是錯的。** 有些規則是樣式比對，確實會誤判。
點該規則的 `spec` 連結看它在執行哪一條要求。如果確定是誤判，記一筆 waiver 寫上理由，
也歡迎帶著案例開 issue。

**修完之後某個 plugin 的工具不見了。** 你把它停用了。
`studio backups` 然後 `studio rollback <id>`，或用 `claude plugin enable <name>` 重新啟用。

**整合顯示「no proposal available」。** PATH 上找不到 `claude` CLI。
除了整合與 `specs --review`，其他功能都不需要它。

## 指令速查

| 指令 | 作用 |
| --- | --- |
| `scan` | 盤點所有設定根目錄，寫出 `var/inventory.json` |
| `health` | 跑完所有規則、存報告、印出結論。失敗時 exit 1 |
| `fix` | 套用只有一種正確答案的修法（`--list`、`--apply`） |
| `consolidate` | AI 規劃的整合，套用前先驗證（`--apply`、`--only`、`--limit`） |
| `update` | 呼叫各套件自己的更新程式（`--only`、`--apply`） |
| `specs` | 檢查引用的規範是否變動（`--review`、`--accept`） |
| `sync` | 把 canonical 來源渲染到各 runtime（`--apply`） |
| `graph` | 以 JSON 輸出關聯圖（`--expand-plugins`） |
| `usage` | 從本機歷史建立呼叫索引 |
| `updates` | 比對已安裝的 plugin 與 toolkit 對遠端版本 |
| `apply <payload>` | 套用已存檔的 change set |
| `backups` / `rollback <id>` | 列出與還原備份 |
| `serve` | 啟動儀表板（`--allow-actions`、`--port`） |
