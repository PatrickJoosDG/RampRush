
# 🚛 Inbound Ramp Challenge — RampRush AI Receiving Sprint

> *Die Rampe ist dein Reich. LKWs rollen heran. Wilde Signale flattern herein. E-Mails auf Französisch, verrauschte Audio-Mitteilungen und Fotos von beschädigten Kartons. Irgendwo da draußen tüftelt ein anderes Team an einem AI-Agenten, der deine Zuweisungen alt aussehen lassen will.*
>
> **Deine Aufgabe: Bau den cleversten Ramp Manager des RampRush Sprints. Schlag alle. Hol dir den Titel.**

In diesem Sprint entwickelst du einen intelligenten **Client-Agenten**, der sich mit dem zentralen Event-Server verbindet, verrauschte Dokumentation programmatisch parst, strukturierte Daten extrahiert und Echtzeit-Entscheidungen zur Zuweisung von Entladungsrampen trifft. Wer am Ende die meisten Punkte hat, gewinnt!

---

## Das Szenario

Ein Logistikzentrum empfängt laufend LKWs. Jede Ankunft ist von unstrukturierter Dokumentation begleitet:
1. **Parcel Photos** (Bilder von Lieferungen, manche zeigen Transportschäden).
2. **Supplier Emails** (Freitext, oft mehrsprachig in DE, FR, IT oder EN, mit Tippfehlern und Abkürzungen).
3. **Supplier Audio Messages** (Sprachnachrichten, oft mit Akzent und Hintergrundrauschen).

Dein AI-Agent muss diese Dokumentation verarbeiten und den LKW einer von **8 spezialisierten Rampen (R01–R08)** zuweisen.

---

## Rampen-Konfiguration

Jede Rampe hat bestimmte Fähigkeiten. Eine Fehlzuweisung kostet Punkte!

| Rampe | Fähigkeit / Einsatzzweck | Spezialisierung / Bedingungen |
|---|---|---|
| **R01–R02** | Paket-Lanes | Für Paket-Lieferungen (`unit: "parcels"`). *Ausnahme: Perishable-Güter müssen zu R07!* |
| **R03–R04** | Standard-Lanes | Für normale LKW-Lieferungen mit maximal 32 Paletten. |
| **R05–R06** | Heavy-Lanes | Primär für übergroße Güter (`goods_type: "oversized"`), können aber auch normale Paletten (≤ 32) aufnehmen. |
| **R07** | Cold Chain | Primär für Kühlware (`goods_type: "perishable"`), kann aber auch normale Paletten (≤ 32) aufnehmen. Kühlware MUSS immer hierhin! |
| **R08** | Double Truck Lane | Ausschließlich für Doppel-LKWs mit mehr als 32 Paletten (`unit: "pallets"`, count > 32). |

> ⚠️ **Business Rule:** LKWs mit Transportschäden (`has_damage=true`) dürfen **nicht** einer Rampe zugewiesen werden. Sie müssen über `/reject-truck` abgelehnt werden. Eine Zuweisung trotz Schaden wird bestraft.

---

## API Reference

**Format:** JSON. Keine Authentifizierung nötig, deine `team_id` identifiziert dich.

### 1. Lieferantenliste abrufen
```
GET /suppliers
```
Gibt ein JSON-Array aller registrierten Lieferanten zurück. Nutze diese Liste für Named-Entity-Resolution (Matching der unstrukturierten Namen auf die canonical ID).
```json
[
  { "supplier_id": 1000000, "supplier_name": "Müller Logistics AG" },
  { "supplier_id": 1000001, "supplier_name": "FastFreight GmbH" }
]
```

### 2. Event-Stream (WebSocket)
```
wss://truckgenerator-production.up.railway.app/ws?team_id=<dein_team_id>
```
Stellt eine dauerhafte Verbindung her. Der Server schickt den nächsten Truck erst, sobald du auf den aktuellen geantwortet hast. Der aktuelle Rampenstatus (Belegung, Queue-Länge) wird mit jedem Truck mitgeliefert — du musst keinen eigenen State aufbauen.

**WebSocket Nachricht vom Server:**
```json
{
  "truck_id": "TRK-042",
  "priority": "high",
  "ramp_status": [
    { "ramp": "R01", "status": "free",     "queue_length": 0 },
    { "ramp": "R02", "status": "occupied", "queue_length": 3 },
    { "ramp": "R07", "status": "occupied", "queue_length": 1 },
    { "ramp": "R08", "status": "free",     "queue_length": 0 }
  ],
  "documentation": [
    { "type": "photo", "url": "https://truckgenerator-production.up.railway.app/assets/photo/TRK-042.jpg" },
    { "type": "email", "text": "Bonjour, livraison de 30 colis de marchandise standard prévue ce matin..." },
    { "type": "audio", "url": "https://truckgenerator-production.up.railway.app/assets/audio/TRK-042.mp3" }
  ]
}
```

### 3a. Truck zuweisen — kein Schaden
```
POST https://truckgenerator-production.up.railway.app/assign-ramp
```
```json
{
  "truck_id": "TRK-042",
  "team_id": "mein-team-name",
  "supplier_id": 1000000,
  "supplier_name": "Müller Logistics AG",
  "parcel_count": 30,
  "has_damage": false,
  "unit": "pallets",
  "assigned_ramp": "R03"
}
```

### 3b. Truck ablehnen — Schaden erkannt
```
POST https://truckgenerator-production.up.railway.app/reject-truck
```
```json
{
  "truck_id": "TRK-042",
  "team_id": "mein-team-name",
  "supplier_id": 1000000,
  "supplier_name": "Müller Logistics AG",
  "parcel_count": 30,
  "has_damage": true,
  "unit": "pallets"
}
```

**Response (beide Endpoints):**
```json
{
  "truck_id": "TRK-042",
  "extraction_score": 40,
  "decision_score": 20,
  "throughput_bonus": 2,
  "total": 62,
  "breakdown": {
    "supplier_id":  { "result": "korrekt (1000000)",                          "earned": 15, "max": 15 },
    "parcel_count": { "result": "korrekt (30)",                               "earned": 10, "max": 10 },
    "has_damage":   { "result": "korrekt (true)",                             "earned": 10, "max": 10 },
    "unit":         { "result": "korrekt (pallets)",                          "earned":  5, "max":  5 },
    "decision":     { "result": "korrekt: Schaden extrahiert, Truck abgelehnt", "earned": 20, "max": 20 },
    "throughput":   { "result": "verarbeitet",                                "earned":  2, "max":  2 }
  }
}
```

---

## Scoring System

Jede Antwort wird sofort bewertet und das Live-Ranking aktualisiert.

### 1. Extraktions-Genauigkeit

Falsche Werte geben **negative Punkte** — Raten ohne Qualität kostet mehr als es bringt.

| Feld | Korrekt | Falsch |
|---|---|---|
| `supplier_id` | **+15** | **−15** |
| `parcel_count` | **+10** | **−10** |
| `has_damage` | **+10** | **−10** |
| `unit` | **+5** | **−5** |

> `supplier_name` wird im Breakdown angezeigt, aber **nicht bewertet** (nur zur Fehlersuche).

### 2. Entscheidung

> **Wichtig:** Die Entscheidung wird gegen deine **extrahierten Werte** geprüft, nicht gegen die Ground Truth. Extraktionsfehler werden so nur einmal bestraft.

| Situation | Punkte |
|---|---|
| `has_damage: true` extrahiert → `/reject-truck` | **+20** |
| `has_damage: false` extrahiert → `/assign-ramp`, Rampe frei | **+7** |
| `has_damage: false` extrahiert → `/assign-ramp`, Belegte Rampe, aber keine freie Alternative | **+7** |
| `has_damage: false` extrahiert → `/assign-ramp`, Rampe belegt obwohl freie Alternative vorhanden | **+0** |
| Richtige Rampenkategorie (Kühlware → R07, Sperrgut → R05/R06, etc.) | **+5** |
| `has_damage: true` extrahiert → `/assign-ramp` (Widerspruch zur eigenen Extraktion) | **−10** |
| `has_damage: false` extrahiert → `/reject-truck` (Ablehnung ohne Schadenserkennung) | **−10** |

> ℹ️ Die **Goods Type**-Kategorie (`standard | oversized | perishable`) musst du aus den Signalen ableiten. Sie wird nicht direkt bewertet, bestimmt aber welche Rampen gültig sind.

### 3. Durchsatz-Bonus (immer +2 Punkte)

Für jeden verarbeiteten Truck gibt es +2 Punkte — unabhängig vom Ergebnis.

### Punktzahl pro Truck

| Szenario | Punkte |
|---|---|
| Beschädigter Truck, alles korrekt extrahiert + abgelehnt | **+62** (40 + 20 + 2) |
| Normaler Truck, alles korrekt + freie Rampe + richtige Kategorie | **+54** (40 + 7 + 5 + 2) |
| Alles falsch extrahiert + inkonsistente Entscheidung | **−34** (−26 − 10 + 2) |

---

*Viel Erfolg beim Inbound Ramp Sprint! Möge der beste AI-Agent gewinnen!* 🏆
