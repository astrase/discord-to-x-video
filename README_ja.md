# Discord X（Twitter）・Pixiv 画像／動画／音声自動ダウンロードBot

Windows 11上で動作するローカルBotです。指定したDiscordチャンネルにX/Twitterの投稿リンク、FixupX/FxTwitter系リンク（`fixupx.com`、`xfixup.com`、`fxtwitter.com`、`twittpr.com`）、録音公開済みのX Spacesリンク（`https://x.com/i/spaces/スペースID`）、またはPixivの作品リンク（`https://www.pixiv.net/artworks/作品ID`）が投稿されると、メディア種別を自動判定してダウンロードし、**画像・動画・音声ファイルと元リンクを同じDiscordメッセージに添付して投稿**します。Xの動画・音声は主にyt-dlp、X画像とPixiv作品はgallery-dlを使用します。監視対象チャンネルでは通常ユーザーの投稿に加え、Webhookが送信したメッセージ本文・Embed内のリンクも検出します。Discordのリンク埋め込みプレビューは抑制し、取得できた投稿者名・アカウント名・投稿本文をテキストとして追加し、リンクは「ソース: URL」と表示します。プロフィールURLと投稿日・時刻は表示しません。アップロードが成功すると、元のリンクメッセージを自動削除します（削除権限が必要です）。

- 無料のPythonライブラリを使用します。外部ストレージや動画共有サイトへの再アップロードは行いません。
- 監視チャンネルは `config.json` またはDiscordのスラッシュコマンドで登録したチャンネルだけです。初期設定済みのチャンネルはそのまま利用でき、Botから追加・削除できます。
- 画像・動画・音声を取得できるのは、利用者がアクセス権を持ち、それぞれyt-dlp/gallery-dlが取得できる投稿に限られます。非公開・削除済み・認証が必要な投稿のアクセス制限を回避しません。
- Xの認証が必要な場合に備え、`config.json` の既定設定ではFirefoxのCookieを試し、年齢制限・ログイン・Cookie関連のエラー時にはBraveを試します。両方のブラウザーで自分のXアカウントにログインしておいてください。Cookie値そのものはファイルに書き出しません。Pixivのgallery-dl抽出器はOAuth refresh tokenが必要なため、Pixivを使う場合は `pixiv_oauth.bat` で利用者自身のアカウントを認可し、返された値を `.env` の `PIXIV_REFRESH_TOKEN` に設定してください。トークン値は `config.json` に書かず、ログにも記録しません。

**BraveのCookieエラーについて:** Windowsで `Could not copy Chrome cookie database` と表示された場合、yt-dlpがBraveのCookieデータベースをコピーできていません。ブラウザーを終了しても直らない場合は、FirefoxでCookieを `cookies.txt` にエクスポートし、下記の「Cookieファイルを使う」を設定してください。Botと同じフォルダーに置いたCookieファイルはログイン情報そのものなので、他人に送ったり、スクリーンショットに写したりしないでください。

### Cookieファイルを使う（ブラウザーDBの読み取りに失敗する場合）

1. FirefoxでXにログインし、対象の投稿がブラウザーで見られることを確認します。
2. [yt-dlp Wikiが案内するFirefox用 `cookies.txt` 拡張機能](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/) など、信頼できる拡張機能を使ってNetscape形式のCookieファイルをエクスポートします。拡張機能には保存済みCookieを読む権限が必要です。必要なドメインだけを出力できる場合は、X関連ドメインに限定してください。
3. 出力したファイルをBotの `bot.py` と同じフォルダーに移動し、名前を `cookies.txt` にします。
4. `config.json` の `cookies_file` を次のように変更します。

   ```json
   "cookies_file": "cookies.txt"
   ```

5. Botを再起動して、同じリンクを再試行します。Botはまず `cookies.txt` を使い、それで認証できない場合は、設定済みのFirefox、Braveの順に試します。

`cookies.txt` はXのログイン状態を引き継げる秘密情報です。ZIPに入れない、Discordやチャットへアップロードしない、Gitなどへ登録しないでください。不要になったら削除し、ログイン状態が変わったらFirefoxから再エクスポートしてください。

## 1. 必要なもの

1. Windows 11 64bit
2. Python **3.11 64bit** と Python Launcher（`py.exe`）
3. Discordアカウントと、自分が管理するDiscordサーバー
4. Discord Developer Portalで作成したBot
5. FFmpegとFFprobe（動画・音声の結合や圧縮を使う場合に必要）

### Pythonの確認

コマンドプロンプトで次を実行します。

```bat
py -0p
py -3.11 --version
```

`py -3.11 --version` が失敗する場合、`setup.bat` は別のPythonバージョンへ勝手に切り替えません。Python 3.11 64bitをインストールしてから再実行してください。Python Install Managerのバージョンによっては `py install 3.11` が使えます。

公式サイト: https://www.python.org/downloads/release/python-311/

## 2. Discord Botの作成と権限設定

1. https://discord.com/developers/applications を開き、**New Application** でアプリを作成します。
2. 左側の **Bot** でBotを作成し、Bot Tokenをコピーします。トークンはパスワードと同じ秘密情報です。誰にも送らないでください。
3. **Bot > Privileged Gateway Intents** の **Message Content Intent** を有効にして保存します。このBotは投稿本文からX/Pixiv URLを読むために必要です。
4. **OAuth2 > URL Generator** で `bot` と `applications.commands` の両方を選択し、Bot権限は最低限、次を選びます。
   - View Channels（チャンネルを見る）
   - Read Message History（メッセージ履歴を読む）
   - Send Messages（メッセージを送信）
   - Attach Files（ファイルを添付）
   - **Manage Messages（メッセージを管理：元リンク投稿を削除するために必要）**
5. 生成されたURLから、自分が管理するサーバーへBotを招待します。監視したいチャンネルで上記権限が許可されていることを確認してください。

管理者権限を付与する必要はありません。Botを利用してよいチャンネルと投稿だけに限定してください。元のリンク投稿を自動削除するには、監視対象チャンネルでBotに「メッセージを管理（Manage Messages）」権限が必要です。起動時に権限不足をログへ警告し、メディア投稿後に削除できなかった場合はDiscordにも理由を通知します。権限がない場合はメディア投稿自体は行いますが、元投稿は残ります。メディアの取得・アップロードに失敗した場合も元投稿は削除せず、再試行できるようにします。複数URLを1つのメッセージに貼った場合、すべてのリンクのメディア投稿が成功したときだけ元投稿を削除します。

## 3. セットアップ

1. ZIPを展開します。例: `C:\DiscordXVideoBot\`
2. フォルダー内の `setup.bat` をダブルクリックします。コマンド画面はセットアップ終了後も開いたままになるため、エラー表示を確認できます。
3. セットアップが最後まで成功したことを確認します。
4. `config.json` と `.env` を編集します（次の章を参照）。

`setup.bat` は画面がすぐ閉じないように `setup_main.bat` を開いたまま実行します。`setup_main.bat` は `.venv` という専用Python仮想環境を作り、`requirements.txt` のパッケージをインストールします。既存の `.env` は上書きしません。

## 4. BotトークンとチャンネルIDの設定

### `.env`

作成された `.env` をメモ帳で開き、次のようにBot Tokenを入力します。

```dotenv
DISCORD_BOT_TOKEN=ここにDiscordのBotトークン
PIXIV_REFRESH_TOKEN=
```

Pixivを使う場合は、初期セットアップ完了後、Botを停止して `pixiv_oauth.bat` を実行します。gallery-dlの認可手順に従い、表示されたrefresh tokenを `.env` の `PIXIV_REFRESH_TOKEN=` の右側に貼り付けて保存してください。Pixivを使わない場合は空欄のままで構いません。トークンはアカウントの認可情報なので、他人に送らないでください。gallery-dlのPixiv抽出器がrefresh tokenを要求する点は公式ドキュメントに記載されています: https://gdl-org.github.io/docs/configuration.html#extractor-pixiv-refresh-token

`=` の前後に余計な引用符は付けず、トークン値をログやDiscordへ投稿しないでください。`.env` は `.gitignore` に登録済みです。

### `config.json`

監視するチャンネルIDを指定します。複数指定できます。初期設定後はDiscordのスラッシュコマンドからも追加・削除できます。

```json
"monitored_channel_ids": [123456789012345678, 234567890123456789]
```

### DiscordのBotコマンドから自動変換チャンネルを管理する

Bot起動後、サーバー管理または管理者権限を持つユーザーが次のスラッシュコマンドを使用できます。追加・削除はすぐに反映され、Botの再起動は不要です。変更内容は `config.json` に自動保存されます。

| コマンド | 動作 |
|---|---|
| `/convert_channel_add` | コマンドを実行したチャンネルを追加します。`channel` に別のテキストチャンネルを指定することもできます |
| `/convert_channel_remove` | コマンドを実行したチャンネルを対象から外します。`channel` で別のチャンネルも指定できます |
| `/convert_channel_list` | このサーバーで自動変換の対象になっているチャンネルを表示します |

例: `#x動画保存` で `/convert_channel_add` を実行すると、そのチャンネルへのX/Pixivリンク投稿が自動変換の対象になります。権限のないユーザーは設定を変更できません。最後のチャンネルを削除して対象が0件になった場合、Botはどのチャンネルも監視しません。再び使う場合は、Botが参加しているサーバーのテキストチャンネルで `/convert_channel_add` を実行してください。

スラッシュコマンドが見えない場合は、Botを再起動して数分待ち、サーバーのアプリ連携・コマンド権限と、Bot招待時の `applications.commands` スコープを確認してください。

チャンネルIDの取得方法:

1. Discordの **ユーザー設定 > 詳細設定 > 開発者モード** を有効にします。
2. 対象チャンネルを右クリックして **チャンネルIDをコピー** を選びます。
3. `config.json` の `monitored_channel_ids` に数字として貼り付けます。IDの前後に `< >` は不要です。

設定を変えたらBotを再起動してください。チャンネルIDを空のまま起動した場合、設定不足のエラーで終了します。全チャンネルを自動監視することはありません。

### DiscordのBotコマンドから投稿表示項目を設定する

サーバー管理または管理者権限を持つユーザーは、Botを再起動せずにチャンネルごとの表示項目を変更できます。設定は `config.json` に自動保存されます。

| コマンド | 動作 |
|---|---|
| `/display_profile_list` | 指定チャンネルの現在の設定と、利用できる表示プロファイルを表示します |
| `/display_profile_set` | `archive` や `links_only` などのプロファイルをチャンネルに適用します |
| `/display_profile_create` | 投稿者名・アカウント名・投稿本文・元リンクの表示／非表示を選んで、独自プロファイルを作成または更新します |
| `/display_items_set` | 現在のチャンネル（または指定チャンネル）の表示項目だけを個別に変更します |

使い方の例:

1. `#保存先` で `/display_profile_list` を実行し、既存の `archive` と `links_only` の内容を確認します。
2. `/display_profile_set profile_name: links_only` を実行すると、そのチャンネルは元リンクだけを表示します。
3. 項目を個別に調整する場合は `/display_items_set show_author_name: false show_account_name: false show_post_text: true show_original_link: true` のように指定します。指定しなかった項目は現在の設定を維持します。
4. 独自プリセットを作る場合は `/display_profile_create` でプロファイル名と4つの表示項目を指定します。作った後に `/display_profile_set` でチャンネルへ適用できます。

`/display_profile_create` で既存のプロファイル名（例: `archive`）を指定すると、そのプロファイルを更新できます。同じ名前のプロファイルを割り当てたチャンネルには更新が反映されます。`/display_items_set` は選択中チャンネルだけの個別設定として保存されます。どの表示コマンドも「サーバー管理」または「管理者」権限が必要です。

### チャンネルごとに投稿表示を変える（複数チャンネルで設定を共有）

`config.json` の `display_settings` では、名前付きの `groups`（表示グループ）を定義し、複数チャンネルに同じグループ名を割り当てられます。例えば、2つのチャンネルを同じ `archive` グループに入れれば、表示設定を1か所だけ変更すれば両方に反映されます。

```json
"display_settings": {
  "default": {
    "show_author_name": true,
    "show_account_name": true,
    "show_post_text": true,
    "show_original_link": true,
    "heading": "X投稿情報"
  },
  "groups": {
    "archive": {
      "show_author_name": true,
      "show_account_name": true,
      "show_post_text": true,
      "show_original_link": true,
      "heading": "動画アーカイブ"
    },
    "links_only": {
      "show_author_name": false,
      "show_account_name": false,
      "show_post_text": false,
      "show_original_link": true,
      "heading": "リンク一覧"
    }
  },
  "channels": {
    "1563019490688761916": "archive",
    "1562916824792825957": "links_only",
    "123456789012345678": "archive"
  }
}
```

この例では `1563019490688761916` と `123456789012345678` の2チャンネルが同じ `archive` 表示になります。IDは例としての値なので、自分のチャンネルIDに置き換えてください。

- `groups` は表示設定の名前と設定内容を定義します。
- `channels` はチャンネルIDからグループ名へ割り当てます。
- `channels` の値を文字列（例：`"archive"`）にすると、そのチャンネルはグループの設定を使います。
- 既存のようにチャンネルIDの値をオブジェクトにして、チャンネル固有の項目を直接指定することもできます。
- グループを基本に一部だけ変えたい場合は `{ "group": "archive", "show_post_text": false }` のように指定します。チャンネル固有の設定がグループ設定より優先されます。
- `monitored_channel_ids` に入っていないチャンネルは、表示設定を書いても監視されません。監視対象にしたいIDは `monitored_channel_ids` に追加するか、Discordの `/convert_channel_add` で登録してください。Botコマンドでの変更は `config.json` に自動保存されます。

| 項目 | `true` / 値を指定した場合 | `false` / 空文字の場合 |
|---|---|---|
| `show_author_name` | 投稿者の表示名を表示 | 表示しない |
| `show_account_name` | `@ユーザー名` を表示 | 表示しない |
| `show_post_text` | Xの投稿本文を表示 | 表示しない |
| `show_original_link` | 元のXリンクを表示 | 表示しない |
| `heading` | メッセージ冒頭に指定した見出しを表示 | `""` なら見出しなし |

`default` は全監視チャンネルに適用され、`channels` に指定したIDだけ設定が上書きされます。例えば特定チャンネルに `"show_post_text": false` だけ指定すれば、そのチャンネルでは本文だけ非表示にし、ほかの項目は共通設定のままです。`config.json` を直接編集した場合はBotを再起動してください。Discordの表示コマンドで変更した場合は即時反映されます。これらはBotが添付メッセージに付けるテキストの表示設定で、メディア取得・元投稿削除・埋め込み抑制の動作は変わりません。

## 5. FFmpegとFFprobeの設定

FFmpegはyt-dlpが別々の映像・音声ストリームを結合するときや、大きな動画・音声を圧縮するときに使います。FFprobeはメディアの長さを取得し、圧縮ビットレートを計算するために使います。

1. Windows用FFmpegビルドを入手します: https://ffmpeg.org/download.html#build-windows
2. `ffmpeg.exe` と `ffprobe.exe` がある場所を確認します。
3. そのフォルダーをWindowsのPATHに追加するか、`config.json` のパスを指定します。例:

```json
"ffmpeg_path": "C:/tools/ffmpeg/bin/ffmpeg.exe",
"ffprobe_path": "C:/tools/ffmpeg/bin/ffprobe.exe"
```

空文字 `""` の場合、PATHから探します。パス中のバックスラッシュを使う場合は `\\` と二重にするか、上のように `/` を使ってください。FFmpegがなくても取得できるメディアはありますが、結合・圧縮ができず、取得品質や形式が制限される場合があります。

確認コマンド:

```bat
ffmpeg -version
ffprobe -version
```

## 6. 起動・停止・更新

- 起動: `start.bat` をダブルクリックします。
- 停止: 別のウィンドウから `stop.bat` を実行します。Botは新しいリンクの受付を止め、キューの処理終了を待ってから停止します。`shutdown_drain_timeout_seconds` を超えた場合も、実行中のダウンロード／圧縮と一時ファイルの後始末が終わるまで待機します。
- キーボードで停止: 起動中の黒いウィンドウで `Ctrl+C` を押します（Botは停止要求を出し、キューの処理終了を待ちます）。
- 更新: Botを停止してから `update.bat` を実行します。discord.py / gallery-dl / python-dotenvに加え、Xなど仕様変更の多いサイトへの対応のため、yt-dlpのnightly版へ更新します。yt-dlp公式はサイト側の変更でstable版が動かなくなる場合があるとして、通常利用者にもnightlyを推奨しています（https://github.com/yt-dlp/yt-dlp#update-channels）。
- 設定診断: 設定済みの状態で次を実行します。

```bat
.venv\Scripts\python.exe bot.py --check
```

Botは多重起動をOSのファイルロックで防止します。`.bot.lock` というファイルが残っていても、プロセス終了後はOSのロックが解放されます。ログは `logs\bot.log` に保存し、一定サイズでローテーションします。

## 7. 設定一覧

`config.json` の主な項目:

| 設定 | 説明 |
|---|---|
| `monitored_channel_ids` | 監視対象のチャンネルID。空配列 `[]` は全チャンネル監視ではなく、監視停止を意味します。Discordのスラッシュコマンドからも変更できます |
| `temp_dir` | 一時保存先。安全のためプロジェクト内の相対パスのみ許可 |
| `max_concurrent_downloads` | 同時ダウンロード数 |
| `max_queue_size` | 待機キューの最大件数。満杯なら通知して受け付けません |
| `max_download_size_mb` | ダウンロードする各メディアファイルの上限MiB相当値。既定8GB。上限を超える取得は失敗扱い |
| `cookies_from_browsers` | 既定 `["firefox", "brave"]`。配列の順番でログイン済み自分のブラウザーCookieを試します。認証・年齢制限関連で失敗した場合は次のブラウザーを試します。Cookie値そのものは保存しません |
| `cookies_from_browser` | 旧設定との互換用。`cookies_from_browsers` がない古い設定でのみ使用します |
| `max_upload_size_mb` | Bot側で設ける追加上限。`0` はDiscordのサーバー情報に基づく上限を優先 |
| `fallback_upload_limit_mb` | Discordの上限情報が取得できない場合の保守的な代替上限 |
| `compress_if_too_large` | 上限超過時の動画・音声のFFmpeg圧縮を有効にするか（画像は自動圧縮しません） |
| `use_gpu_decode` | Windowsの動画圧縮時にD3D11VA GPUデコードを先に試す（既定 `true`）。失敗するとCPUデコードに自動フォールバック。エンコードはCPUの `libx264` |
| `compression_attempts` | 圧縮を試す回数（既定8回）。圧縮後も大きい動画は複数パートに分割 |
| `max_split_parts` | 長い動画を分割するときの最大パート数（既定100）。10ファイルずつ複数メッセージで送信 |
| `compression_audio_bitrate_kbps` | 圧縮後の音声ビットレート |
| `compression_min_video_bitrate_kbps` | 圧縮後の映像ビットレートの下限（既定100kbps） |
| `ffmpeg_path` / `ffprobe_path` | 実行ファイルのパス。空ならPATHから検索 |
| `log_level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `deduplicate_posts` | 成功済みの同一X投稿ID／Pixiv作品IDを再アップロードしない |
| `reply_with_progress` | ダウンロード中の進捗を一時的に返信する（成功時は削除） |
| `delete_source_message_on_success` | メディア投稿が成功したら元のリンクメッセージを削除する（既定 `true`）。すべてのリンク処理が成功した場合のみ削除 |
| `shutdown_drain_timeout_seconds` | stop.batで停止するときキューを待つ上限秒数 |

Discordのアップロード上限はアカウントやサーバーの状態・段階的な仕様変更で異なる場合があります。2026年10月時点のDiscord公式FAQでは標準の無料アップロード上限を20MBと案内しています（[Discord公式FAQ](https://support.discord.com/hc/ja/articles/25444343291031-%E6%B7%BB%E4%BB%98%E3%83%95%E3%82%A1%E3%82%A4%E3%83%AB-FAQ)）。Botはサーバーのファイルサイズ上限も参照しますが、discord.py内の上限表が最新仕様より遅れる場合を考慮して標準上限を20MiB未満にはしません。必要なら `max_upload_size_mb` でさらに小さく制限します。実際のAPIがファイルを拒否した場合は失敗を通知し、上限を無理に突破しません。Discord側が実際のアップロードを拒否した場合もログに記録し、無制限に再試行しません。

大きな動画は、圧縮設定が有効でFFmpeg/FFprobeがある場合にだけ別ファイルへH.264/AAC MP4として圧縮します。Windowsでは既定でD3D11VAによるGPUデコードを先に試し、非対応コーデック・ドライバー・FFmpegビルドなどで失敗した場合はCPUデコードへ自動で切り替えます。映像エンコード自体は `libx264`（CPU）です。GPUエンコードではありません。大きな音声も同条件で別のAAC/M4Aファイルへ圧縮します。元ファイルを上書きしません。圧縮しても1ファイルの上限に収まらない動画は、ストリームコピーで複数のMP4パートへ分割し、10ファイルずつ複数メッセージで送信します（既定最大100パート）。分割できない場合や設定上限を超える場合は失敗を通知します。圧縮は画質を下げる場合があります。

## 8. 対応するメディア

- **動画付き投稿**：yt-dlpでMP4などの動画を取得します。FFmpegがあれば映像と音声ストリームを結合できます。
- **画像のみの投稿**：yt-dlpでメディアを取得できない場合にgallery-dlへ切り替え、最大10枚の画像を取得します。画像は1メッセージに添付して送信します。
- **音声**：音声ファイルを返す投稿や、公開済み・録音済みのX Spaces直リンク（`https://x.com/i/spaces/スペースID`、`https://twitter.com/i/spaces/スペースID`）をyt-dlpで取得できれば音声ファイルとして送信します。現在配信中のSpaces、録音が公開されていないSpaces、認証が必要な投稿は取得できない場合があります。
- **サイズ制限**：動画はこれまで通りFFmpegで圧縮できます。音声も上限を超えた場合はFFmpegでAAC/M4Aに圧縮を試みます。画像の自動圧縮は未対応で、上限を超える画像はアップロードせずエラーにします。
- **Pixiv作品**：`gallery-dl` でイラスト・漫画の画像を取得します。うごイラはFFmpegが検出されたとき `--ugoira mp4` でMP4変換を試みます。小説リンクは表紙画像の取得対象で、小説本文をテキストファイルとしてDiscordへ転送する処理ではありません。PixivのOAuth tokenが必要です。
- Discordの1メッセージに添付できるファイル数の上限を考慮し、10個を超える画像は一部だけを投稿して元リンクを消すことがないよう失敗扱いにします。

## 9. 投稿テキストの表示

Discordの埋め込みプレビューは表示せず、メディア添付メッセージにX投稿の情報をテキストとして含めます。yt-dlpが取得できた項目に応じて、投稿者名、アカウント名、投稿本文、ソースリンク（「ソース: URL」）を表示します。プロフィールURLと投稿日・時刻は表示しません。取得できない項目は省略し、長い投稿本文はDiscordの文字数制限に収まるよう末尾を省略します。いいね数などの数値はサイトやyt-dlpの取得結果によって取れないため、必須項目にはしていません。

## 10. 処理の安全性と重複防止

- `x.com` / `twitter.com` と、FixupX/FxTwitter系の `fixupx.com` / `xfixup.com` / `fxtwitter.com` / `twittpr.com` のHTTPS投稿URLだけを処理します。これらは元の `x.com` 投稿URLに正規化してからyt-dlpへ渡します。他ドメイン、ユーザー情報付きURL、ポート付きURL、不正な投稿パスは拒否します。
- Bot自身や他のBotのメッセージは処理しません。
- SQLiteの `state.sqlite3` に投稿IDと状態を記録します。同時処理中・キュー内・成功済みの重複をスキップします。失敗した投稿は再投稿で再試行できます。
- 一時ファイルは投稿ごとの専用サブフォルダーに保存し、処理終了時に削除します。プロセス異常終了やWindowsのファイルロックで削除できなかった場合は、`temp` フォルダーを確認して残ったファイルを手動削除してください。
- Discordメッセージから任意のシェルコマンドを実行しません。外部サイトへメディアを転送しません。
- ローカルログにエラーの詳細を記録します。Bot TokenやCookie値をログに記録しません。Cookieは `cookies_from_browsers` に設定されたブラウザーから処理時に直接読み込みます。Cookie値そのものを設定ファイルやログへ保存しません。

利用する動画の著作権、サービス規約、投稿者の権利、所属するサーバーのルールを確認し、取得・再投稿が許可された動画だけを扱ってください。

## 11. トラブルシューティング

**「Python 3.11が見つかりません」**

`py -0p` でインストール済みPythonを確認します。`py -3.11 --version` が通らない場合はPython 3.11 64bitをインストールしてください。このセットアップは違うバージョンへ自動切替しません。

**Botがすぐ終了する / 設定エラー**

`config.json` のJSON記法、`monitored_channel_ids`、`.env` のトークンを確認します。エラー全文は起動ウィンドウと `logs\bot.log` を見てください。スラッシュコマンドが出ない場合は `applications.commands` スコープ、コマンド同期ログ、サーバー側のアプリコマンド権限を確認してください。

**Botはオンラインだがリンクに反応しない**

Developer PortalでMessage Content Intentが有効か、Botが正しいサーバーにいるか、投稿先のチャンネルIDが設定と一致するか、View Channels / Read Message History権限があるか確認します。監視対象外のチャンネルには反応しません。Webhook投稿は `webhook_id` が付いたメッセージを許可します。WebhookのURLがEmbed内だけにある場合も検出しますが、Webhookが送るメッセージ本文／Embedの内容自体に対象URLが含まれていない場合は処理できません。

**ファイル取得エラー**

まずBotを停止し、`update.bat` を実行してyt-dlpのnightly版とgallery-dlを更新し、Botを再起動してください。X側のAPI変更や一時制限により、更新済みでも取得に失敗することがあります。

ログイン済みの自分のブラウザーでは表示できる投稿が匿名では取得できない場合、`config.json` の `cookies_from_browsers` を設定します。既定は `["firefox", "brave"]` で、まずFirefoxを試し、年齢制限・ログイン・Cookie関連の失敗ならBraveのCookieで再試行します。Firefoxのみなら `["firefox"]`、Braveのみなら `["brave"]`、Cookieを使わない場合は `[]` にします。対象ブラウザーはあらかじめXにログインしておいてください。ブラウザーのプロファイルが複数ある場合やブラウザーがロック中の場合、Cookieを読み込めないことがあります。Cookieを他人に送らないでください。

エラー表示にはyt-dlp/gallery-dlの原因概要が含まれ、詳細は `logs\bot.log` に記録されます。まだ失敗する場合は、その投稿へのアクセス権とログ内の `yt-dlpで動画/音声を取得できませんでした` / `gallery-dl` に続く行を確認します。削除済み・非公開・アクセス権のない投稿の制限を回避することはできません。

**Pixivの取得に失敗する / PIXIV_REFRESH_TOKENがない**

Botを停止し、`pixiv_oauth.bat` を実行して、gallery-dlのPixiv OAuth認可をやり直してください。表示されたrefresh tokenを `.env` の `PIXIV_REFRESH_TOKEN=` に貼り付け、保存後に `start.bat` を実行します。トークンは `config.json` やDiscordに貼り付けないでください。トークンは一時的なジョブ用設定ファイルに限って使用し、通常の処理終了後に削除します。認可できない・トークンが失効した場合は再認可してください。

**FFmpeg / FFprobeが見つからない**

PATH、`config.json` の実行ファイルパス、実際にEXEが存在するかを確認してください。圧縮には両方が必要です。

**アップロード上限を超える**

`max_upload_size_mb`、サーバー側の上限、FFmpeg/FFprobe、`compress_if_too_large`、`max_download_size_mb`、`max_split_parts` を確認します。Discordの1ファイル上限自体は設定値だけでは突破できません。Botは上限を自動判定し、動画は圧縮後も大きい場合に複数のMP4パートへ分割します。画像の自動圧縮・分割は未対応です。

**権限不足**

監視チャンネルで View Channels / Read Message History / Send Messages / Attach Files を許可してください。リンク投稿を自動削除する場合は、さらに Manage Messages が必要です。

## 12. テストと制限

`setup.bat` はPython構文チェックと標準ライブラリだけで動くユニットテストを実行します。画像投稿の取得には `gallery-dl` も利用し、`requirements.txt` から自動インストールします。テストはURL検証、重複URLの除去、SQLiteの重複管理、ファイルサイズ補助関数を確認します。

このプロジェクト配布時点では、実際のDiscordサーバーへのログイン、X/Pixiv画像・動画・音声の実取得、Webhookからの本番メッセージ検出、Discordへの本番アップロードをこの環境では行っていません。これらはユーザー自身のBot Token、監視チャンネルID、ネットワーク接続、Discord側の権限、および取得対象投稿へのアクセス可否が必要です。ローカルテスト成功は、本番接続を保証するものではありません。

yt-dlpの情報: https://github.com/yt-dlp/yt-dlp

gallery-dlの情報: https://github.com/mikf/gallery-dl
gallery-dl Pixiv OAuth設定: https://gdl-org.github.io/docs/configuration.html#extractor-pixiv-refresh-token

discord.pyドキュメント: https://discordpy.readthedocs.io/
