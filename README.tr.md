<p align="center">
  <img src="docs/logo.svg" width="88" height="88" alt="evren-bridge logosu">
</p>
<h1 align="center">evren-bridge</h1>
<p align="center">
  Her kodlama ajanının oturumunu aynı API anahtarında tutun, tüm ajanları terminalinizden canlı izleyin.
</p>
<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-65d6ce?style=flat-square" alt="MIT lisansı"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependency-rich-f1b66b?style=flat-square" alt="Bağımlılık: rich">
  <img src="https://img.shields.io/badge/OpenAI-Chat_Completions-64748b?style=flat-square&amp;logo=openai&amp;logoColor=white" alt="OpenAI Chat Completions uyumlu"><br>
  <a href="README.md">English</a> · Türkçe
</p>

Prompt cache çoğunlukla hesap başına tutulur: bir ajanın istekleri başka bir anahtara geçerse önbellekteki girdisi kaybolur. evren-bridge her ajanın oturumunu aynı anahtarda tutar; her ajanın ne yaptığını, ne hızda yanıt verdiğini ve girdisinin ne kadarının önbellekten geldiğini gösterir.

[EVREN](https://evren.ssyz.org.tr) için geliştirildi; OpenAI Chat Completions sunan her sağlayıcıyla çalışır. EVREN'in resmî bir projesi değildir.

![Windows Terminal'de evren-bridge](docs/screens/terminal.png)

## Hızlı başlangıç

Windows, macOS veya Linux'ta Python 3.10+ gerekir (`python` bulunamazsa `python3` kullanın).

```sh
git clone https://github.com/Kscl1/evren-bridge
cd evren-bridge
python -m pip install -r requirements.txt
```

`~/.evren/keys.txt` dosyasını (Windows: `%USERPROFILE%\.evren\keys.txt`) ve bulunduğu dizini oluşturun; dosyaya API anahtarınızı yazın:

```text
main=sk-...
```

```sh
python evren_bridge.py
```

Bu komut köprüyü EVREN için başlatır. Başka bir sağlayıcı için `--upstream URL` ekleyip `/v1` olmadan sağlayıcının kök URL'sini verin.

Ajanınızın OpenAI uyumlu base URL ayarını `http://127.0.0.1:8787/v1`, API anahtarını `unused` yapın; köprü bunun yerine sizin anahtarınızı kullanır.

Paneli API anahtarı olmadan deneyin: `python docs/demo.py`.

## İstemciler

Her istemcide yukarıdaki base URL ile Chat Completions / OpenAI uyumlu sağlayıcıyı kullanın.

| İstemci | Oturumu aynı anahtarda tutmak için ayar |
|---|---|
| <img src="https://github.com/earendil-works.png?size=40" width="20" height="20" alt=""> [Pi](https://github.com/earendil-works/pi) | Her modelde `sendSessionAffinityHeaders` açılmalı, aşağıya bakın |
| <img src="https://github.com/anomalyco.png?size=40" width="20" height="20" alt=""> [OpenCode](https://github.com/anomalyco/opencode) | Otomatik |
| <img src="https://github.com/Kilo-Org.png?size=40" width="20" height="20" alt=""> [Kilo Code](https://github.com/Kilo-Org/kilocode) (OpenCode tabanlı) | Otomatik |
| <img src="https://github.com/charmbracelet.png?size=40" width="20" height="20" alt=""> [Crush](https://github.com/charmbracelet/crush) | Henüz test edilmedi |
| <img src="https://github.com/aaif-goose.png?size=40" width="20" height="20" alt=""> [Goose](https://github.com/aaif-goose/goose) | Henüz test edilmedi |

<img src="https://github.com/cline.png?size=40" width="20" height="20" alt=""> [Cline](https://github.com/cline/cline), <img src="https://github.com/RooCodeInc.png?size=40" width="20" height="20" alt=""> [Roo Code](https://github.com/RooCodeInc/Roo-Code), <img src="https://github.com/continuedev.png?size=40" width="20" height="20" alt=""> [Continue](https://github.com/continuedev/continue), <img src="https://github.com/Aider-AI.png?size=40" width="20" height="20" alt=""> [Aider](https://github.com/Aider-AI/aider), <img src="https://github.com/zed-industries.png?size=40" width="20" height="20" alt=""> [Zed](https://github.com/zed-industries/zed) ve <img src="https://github.com/QwenLM.png?size=40" width="20" height="20" alt=""> [Qwen Code](https://github.com/QwenLM/qwen-code) çalışır, ancak oturum kimliği göndermez; bu yüzden aynı anahtarda tutulmazlar.

<details>
<summary>Pi ayarları</summary>

`~/.pi/agent/models.json`:

```json
{
  "providers": {
    "evren-bridge": {
      "baseUrl": "http://127.0.0.1:8787/v1",
      "api": "openai-completions",
      "apiKey": "unused",
      "models": [
        { "id": "glm-5.3", "compat": { "sendSessionAffinityHeaders": true } }
      ]
    }
  }
}
```

```sh
pi --provider evren-bridge --model glm-5.3
```

</details>

Kendi istemcileriniz `X-Session-Affinity`, `X-Session-Id` veya `Agent-Session-Id` gönderebilir (ilk dolu başlık kullanılır). Codex CLI ve Claude Code başka API'ler kullanır ve desteklenmez.

## Ayarlar

| Ayar | Varsayılan | İşlev |
|---|---|---|
| `--upstream URL` / `EVREN_BRIDGE_UPSTREAM` | `https://evren-llmapi.ssyz.org.tr` | Kök URL; istek yolları sonuna eklenir |
| `--profile evren\|none` | upstream değiştirilmediyse `evren`, aksi hâlde `none` | Sağlayıcıya özgü kurallar |
| `--active-cap N` | `20` | Anahtar başına aktif oturumlar için yerleşim eşiği |
| `--lang en\|tr` | `en` | Panel dili |
| `--no-panel` | Kapalı | Panel yerine istek günlüğü çıktısı |
| `EVREN_KEYS_FILE` | `~/.evren/keys.txt` | Anahtar dosyası |
| `EVREN_BRIDGE_PORT` | `8787` | Yerel port |

`GET /bridge/quota` anahtar etiketlerini ve yönlendirme durumunu, profil sağlıyorsa kota verilerini de döndürür.

## Nasıl çalışır

- Bir oturum, isteği sürerken ve istediği aracı çalıştırırken (araç çağrısı başına en fazla 10 dakika) **aktif**, turu bitince **boştadır**.
- Birden fazla anahtar için aynı sağlayıcının anahtarlarını alt alta `label=key` satırları olarak ekleyin. Yeni oturumlar, 20'den az aktif oturumu olan ilk kullanılabilir anahtara (`--active-cap`), yoksa en az aktif oturumu olan kullanılabilir anahtara gider. Boştaki oturumlar sayılmaz.
- Oturumların anahtar atamaları son etkinliklerinden sonra 60 dakika bellekte tutulur. Geri dönen oturum, eşik dolu olsa bile kendi anahtarına döner; o anahtar kenara alınmışsa dönmez. Yük hiçbir oturumu başka anahtara taşımaz; eşik bir eşzamanlılık sınırı değildir.
- Sağlayıcı profili yoksa upstream yanıt gövdeleri ve hata durum kodları değiştirilmeden iletilir.

## EVREN profili

Upstream'i değiştirmediğiniz sürece varsayılan olarak açıktır; açıkça etkinleştirmek için `--profile evren` kullanın.

| Koşul | Davranış |
|---|---|
| Dakikalık limit (429) | Olduğu gibi iletir; yoksa `Retry-After: 60` ekler |
| Günlük limit (429) | Anahtarı sıfırlanmaya kadar kenara alır; isteği başka bir kullanılabilir anahtarla tekrarlar |
| Bütün anahtarlar kenara alınmış | 429 döndürür; `Retry-After` ilk sıfırlanmaya kalan süredir |
| Akışın ilk veri parçasında gizlenmiş hata | İstemcinin yeniden denemesi için 503 döndürür |

Profil, panele dakikalık, 5 dakikalık ve günlük kota ölçümlerini ekler.

## Panel

| Tuş | İşlev |
|---|---|
| `←` / `→` veya `1` / `2` / `3` | Sekme değiştirir |
| `Tab` | İzleme: aktif / boştaki ajanlar arasında geçer |
| `↑` / `↓`, `PgUp` / `PgDn` | İzleme: ajan sayfaları arasında geçer; İstekler: kaydırır; İstatistikler: zaman aralığını değiştirir |
| `Home` | İzleme: ilk sayfa; İstekler: en yeni istek; İstatistikler: ilk zaman aralığı |
| `L` | İngilizce / Türkçe arasında geçer |
| `q` | Çıkış |

### İzleme

![İzleme](docs/screens/live.svg)

### İstekler

![İstekler](docs/screens/requests.svg)

### İstatistikler

![İstatistikler](docs/screens/stats.svg)

## Gizlilik

Köprü yalnızca `127.0.0.1`'de dinler. Makinenizdeki her program köprüyü kullanabilir; bu yüzden güvendiğiniz bir makinede çalıştırın.
`logs/bridge.log` istek üst verilerini, anahtar etiketlerini ve oturum kimliklerinin son sekiz karakterini kaydeder; API anahtarlarını, mesaj içeriğini veya araç argümanlarını asla kaydetmez. İstatistikler bu günlükten yüklenir; yeniden başlatıldığında oturumların anahtar atamaları kaybolur. Önbellek isabetleri sağlayıcıya bağlıdır.

## Geliştirme

```sh
python -m unittest
```

## Lisans

[MIT](LICENSE)
