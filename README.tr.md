<p align="center">
  <img src="docs/logo.svg" width="88" height="88" alt="evren-bridge logosu">
</p>
<h1 align="center">evren-bridge</h1>
<p align="center">
  EVREN için yerel bir köprü: bütün kodlama ajanlarını canlı izler, EVREN'in limit ve hatalarını yumuşatır.
</p>
<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-65d6ce?style=flat-square" alt="MIT lisansı"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependency-rich-f1b66b?style=flat-square" alt="Bağımlılık: rich">
  <img src="https://img.shields.io/badge/OpenAI-Chat_Completions-64748b?style=flat-square&amp;logo=openai&amp;logoColor=white" alt="OpenAI Chat Completions uyumlu"><br>
  <a href="README.md">English</a> · Türkçe
</p>

evren-bridge, bilgisayarında kodlama ajanlarınla [EVREN](https://evren.ssyz.org.tr)'in, yani Türk Savunma Sanayii Yapay Zeka Platformu'nun LLM servisi arasında çalışır. Terminaldeki paneli her ajanın ne yaptığını, ne hızda yanıt verdiğini, girdisinin ne kadarının önbellekten geldiğini ve günlük kotadan ne kaldığını gösterir.

EVREN'in pürüzlerini de düzeltir: günlük limite takılan istek net bir bekleme süresiyle döner, EVREN'in akış içinde gizlediği hatalar normal bir yeniden denemeye dönüşür.

Başka OpenAI Chat Completions sağlayıcılarıyla da çalışır. EVREN'in resmî bir projesi değildir.

![Windows Terminal'de evren-bridge](docs/screens/terminal.png)

## Hızlı başlangıç

Python 3.10 veya üstü gerekir (macOS ve Linux'ta `python` yoksa `python3`).

```sh
git clone https://github.com/Kscl1/evren-bridge
cd evren-bridge
python -m pip install -r requirements.txt
```

EVREN API anahtarını, önüne bir etiket koyarak `~/.evren/keys.txt` dosyasına yaz (Windows'ta `%USERPROFILE%\.evren\keys.txt`):

```text
main=sk-...
```

Köprüyü başlat ve açık bırak; panel aynı terminalde açılır:

```sh
python evren_bridge.py
```

Ajanında base URL'si `http://127.0.0.1:8787/v1` olan OpenAI uyumlu bir sağlayıcı ekle; API anahtarına ne yazdığın önemli değil, köprü gerçek anahtarını kendisi gönderir.

Henüz anahtarın yok mu? `python docs/demo.py` paneli uydurma ajanlarla gösterir.

## İstemciler

OpenAI Chat Completions konuşan her ajan çalışır. Ajanın kendi anahtarında kalması için ayrıca bir oturum kimliği göndermesi gerekir. Bunlar gönderir:

| İstemci | Oturumu aynı anahtarda tutmak için ayar |
|---|---|
| <img src="https://github.com/earendil-works.png?size=40" width="20" height="20" alt=""> [Pi](https://github.com/earendil-works/pi) | Her modelde `sendSessionAffinityHeaders` açılmalı, aşağıya bakın |
| <img src="https://github.com/anomalyco.png?size=40" width="20" height="20" alt=""> [OpenCode](https://github.com/anomalyco/opencode) | Otomatik |
| <img src="https://github.com/Kilo-Org.png?size=40" width="20" height="20" alt=""> [Kilo Code](https://github.com/Kilo-Org/kilocode) (OpenCode tabanlı) | Otomatik |
| <img src="https://github.com/charmbracelet.png?size=40" width="20" height="20" alt=""> [Crush](https://github.com/charmbracelet/crush) | Henüz test edilmedi |
| <img src="https://github.com/aaif-goose.png?size=40" width="20" height="20" alt=""> [Goose](https://github.com/aaif-goose/goose) | Henüz test edilmedi |

<img src="https://github.com/cline.png?size=40" width="20" height="20" alt=""> [Cline](https://github.com/cline/cline), <img src="https://github.com/RooCodeInc.png?size=40" width="20" height="20" alt=""> [Roo Code](https://github.com/RooCodeInc/Roo-Code), <img src="https://github.com/continuedev.png?size=40" width="20" height="20" alt=""> [Continue](https://github.com/continuedev/continue), <img src="https://github.com/Aider-AI.png?size=40" width="20" height="20" alt=""> [Aider](https://github.com/Aider-AI/aider), <img src="https://github.com/zed-industries.png?size=40" width="20" height="20" alt=""> [Zed](https://github.com/zed-industries/zed) ve <img src="https://github.com/QwenLM.png?size=40" width="20" height="20" alt=""> [Qwen Code](https://github.com/QwenLM/qwen-code) da çalışır, ama oturum kimliği göndermedikleri için tek bir anahtarda tutulmazlar.

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

Başka istemciler `X-Session-Affinity`, `X-Session-Id` veya `Agent-Session-Id` başlığını gönderebilir. Codex CLI ve Claude Code başka API'ler konuşur, çalışmaz.

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

`curl http://127.0.0.1:8787/bridge/quota` her anahtarın durumunu ve EVREN kotasını JSON olarak gösterir. Anahtarın kendisi hiçbir zaman görünmez.

## Nasıl çalışır

- Bir ajan yanıt beklerken ya da istediği aracı çalıştırırken (en fazla 10 dakika) **aktif**, turu bitince **boşta** sayılır.
- Her ajan başladığı anahtarda kalır; böylece EVREN'in prompt cache'i konuşma boyunca korunur. Köprü bu anahtarı 60 dakika hatırlar; bu sürede geri dönen ajan aynı anahtara döner.
- Yoğun bir anahtar hiçbir ajanı başka yere itmez: EVREN'in dakika limiti ajana kendi anahtarında ulaşır.
- EVREN profili kapalıyken yanıtlar ve hatalar olduğu gibi geçer.

## EVREN profili

Varsayılan olarak açıktır. `--upstream` kullanırsan kapanır; açık kalsın istersen `--profile evren` de ekle.

| EVREN'den gelen | evren-bridge |
|---|---|
| Dakika limiti (429) | Olduğu gibi iletir; EVREN `Retry-After` göndermediyse 60 saniye ekler |
| Günlük limit (429) | Anahtarı sıfırlanana kadar kenara alır |
| Başarılı görünen bir akışın içine gizlenmiş hata | 503 döndürür, ajan yeniden dener |

Profil açıkken panel EVREN'in kendi sayaçlarından okunan dakikalık, 5 dakikalık ve günlük kotayı da gösterir.

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

- Yalnızca `127.0.0.1` üzerinde dinler. Bilgisayarındaki her program köprüye, onun üzerinden de anahtarına ulaşabilir.
- `logs/bridge.log` her istek için şunları tutar: zaman, anahtar etiketi, yol, model, durum, token sayıları, süreler ve oturum kimliğinin sonu. Anahtar, mesaj ve araç argümanı tutmaz.
- Hangi ajanın hangi anahtarda olduğu yalnızca bellekte durur; yeniden başlatınca sıfırdan başlar.

## Geliştirme

```sh
python -m unittest
```

Testler yerel sahte sunucularla çalışır, ağa hiç çıkmaz.

## Lisans

[MIT](LICENSE)
