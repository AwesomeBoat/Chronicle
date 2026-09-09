/*
 * Capteur de chambre - ESP32 + BME280  (Chronicle, V4 etape 6)
 * ---------------------------------------------------------------
 * Mesure temperature / humidite / pression toutes les 5 minutes, garde
 * les 24 dernieres heures en memoire, et les sert en JSON quand le PC
 * vient les chercher.
 *
 * POURQUOI IL SERT AU LIEU D'EMETTRE
 * Le PC n'est pas allume en permanence, et c'est la nuit que les
 * mesures comptent. Un capteur qui pousse vers un serveur absent perd
 * exactement ce qu'on veut mesurer. Ici le module accumule, le PC
 * ramasse quand il peut.
 *
 * MATERIEL
 *   ESP32 DevKit v1        ~8 EUR
 *   BME280 (I2C, 3.3V)     ~5 EUR
 *   Cablage : 3V3->VIN, GND->GND, GPIO22->SCL, GPIO21->SDA
 *
 *   Attention : certains BME280 chinois repondent en 0x76, d'autres en
 *   0x77. Le code essaie les deux.
 *
 * BIBLIOTHEQUES (gestionnaire de bibliotheques Arduino)
 *   - Adafruit BME280 Library
 *   - Adafruit Unified Sensor
 *   - ArduinoJson (v7)
 *
 * AVANT DE TELEVERSER : renseigner WIFI_SSID / WIFI_PASS ci-dessous,
 * puis relever l'IP affichee sur le moniteur serie et la mettre dans le
 * .env du projet :
 *
 *   BEDROOM_SENSOR_URL=http://192.168.1.42
 *
 * PLACEMENT DANS LA CHAMBRE
 * Loin d'un radiateur, d'une fenetre et du lit lui-meme : le corps
 * humain chauffe et humidifie son voisinage immediat. A hauteur de lit,
 * a un metre ou deux, contre un mur interieur.
 */

#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include <Adafruit_BME280.h>
#include <ArduinoJson.h>
#include <time.h>

// ------------------------------------------------------------- reglages

const char *WIFI_SSID = "A_REMPLIR";
const char *WIFI_PASS = "A_REMPLIR";

const uint32_t INTERVALLE_MS = 5UL * 60UL * 1000UL;  // une mesure / 5 min

// 288 mesures = 24 h a raison d'une toutes les 5 minutes.
// Chaque entree fait 16 octets, soit ~4,6 Ko : negligeable sur les
// 320 Ko de RAM d'un ESP32. Augmenter ce nombre allonge l'autonomie
// face a un PC eteint - c'est le seul reglage qui compte vraiment.
const size_t CAPACITE = 288;

// ---------------------------------------------------------------- etat

struct Releve {
  uint32_t t;      // epoch UTC, secondes
  float temp;
  float hum;
  float pres;
};

Releve tampon[CAPACITE];
size_t nb_ecrits = 0;      // total cumule depuis le demarrage
uint32_t derniere_mesure = 0;

Adafruit_BME280 bme;
WebServer serveur(80);
bool capteur_present = false;

// ------------------------------------------------------------ utilitaires

// Tampon CIRCULAIRE : quand il est plein, la mesure la plus ancienne est
// ecrasee. C'est le comportement voulu - perdre le vieux plutot que de
// refuser le neuf. Une chambre de 2026-08-20 n'interesse plus personne
// si on est le 25 et que la base a deja tout.
void ajouter(float temp, float hum, float pres) {
  time_t maintenant = time(nullptr);

  // Horloge non synchronisee : on n'enregistre PAS. Une mesure datee de
  // 1970 traverserait toutes les validations cote PC et polluerait une
  // base temporelle. Mieux vaut un trou qu'une fausse date.
  if (maintenant < 1767225600) {   // < 2026-01-01
    Serial.println("[skip] horloge non synchronisee");
    return;
  }

  Releve &place = tampon[nb_ecrits % CAPACITE];
  place.t = (uint32_t)maintenant;
  place.temp = temp;
  place.hum = hum;
  place.pres = pres;
  nb_ecrits++;
}

void mesurer() {
  if (!capteur_present) return;

  float temp = bme.readTemperature();
  float hum = bme.readHumidity();
  float pres = bme.readPressure() / 100.0F;   // Pa -> hPa

  // Un BME280 debranche en cours de route renvoie des NaN. Les laisser
  // passer enverrait "null" dans le JSON et ferait echouer le parsing
  // cote PC.
  if (isnan(temp) || isnan(hum)) {
    Serial.println("[skip] lecture NaN - capteur debranche ?");
    return;
  }

  ajouter(temp, hum, pres);
  Serial.printf("%.2f C  %.1f %%  %.1f hPa  (%u mesures)\n",
                temp, hum, pres, (unsigned)nb_ecrits);
}

// ------------------------------------------------------------- routes

// GET /readings?since=<epoch>
void route_readings() {
  uint32_t depuis = 0;

  if (serveur.hasArg("since")) {
    depuis = (uint32_t)serveur.arg("since").toInt();
  }

  // Assez large pour 288 releves ; ArduinoJson v7 alloue dynamiquement.
  JsonDocument doc;
  doc["device"] = "bedroom";
  doc["now"] = (uint32_t)time(nullptr);
  doc["stored"] = nb_ecrits;

  JsonArray sortie = doc["readings"].to<JsonArray>();

  // Parcours du plus ancien au plus recent. Quand le tampon a deborde,
  // le plus ancien n'est pas l'indice 0 mais nb_ecrits - CAPACITE.
  size_t depart = (nb_ecrits > CAPACITE) ? nb_ecrits - CAPACITE : 0;

  for (size_t i = depart; i < nb_ecrits; i++) {
    const Releve &r = tampon[i % CAPACITE];

    if (r.t <= depuis) continue;   // deja recupere par le PC

    JsonObject o = sortie.add<JsonObject>();
    o["t"] = r.t;
    o["temp"] = round(r.temp * 100) / 100.0;
    o["hum"] = round(r.hum * 100) / 100.0;
    o["pres"] = round(r.pres * 100) / 100.0;
  }

  String corps;
  serializeJson(doc, corps);
  serveur.send(200, "application/json", corps);
}

// GET /  -> etat lisible par un humain, pour diagnostiquer au navigateur
void route_racine() {
  String page = "Capteur chambre\n";
  page += "capteur : " + String(capteur_present ? "OK" : "ABSENT") + "\n";
  page += "mesures : " + String(nb_ecrits) + "\n";
  page += "capacite: " + String(CAPACITE) + " (" +
          String(CAPACITE * 5 / 60) + " h)\n";
  page += "heure   : " + String((uint32_t)time(nullptr)) + "\n";
  serveur.send(200, "text/plain", page);
}

// ------------------------------------------------------------ cycle

void setup() {
  Serial.begin(115200);
  delay(200);

  Wire.begin();

  // Les deux adresses I2C possibles du BME280.
  capteur_present = bme.begin(0x76) || bme.begin(0x77);

  if (!capteur_present) {
    Serial.println("BME280 introuvable - verifier le cablage I2C");
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  Serial.print("WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("IP : ");
  Serial.println(WiFi.localIP());
  Serial.println("-> a mettre dans .env : BEDROOM_SENSOR_URL=http://" +
                 WiFi.localIP().toString());

  // NTP : sans horloge a l'heure, toutes les mesures sont inexploitables.
  // UTC (offset 0) volontairement : le fuseau est applique cote PC, une
  // seule fois, comme partout dans ce projet depuis la V1.
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");

  Serial.print("NTP");
  while (time(nullptr) < 1767225600) {
    delay(500);
    Serial.print(".");
  }
  Serial.println(" ok");

  serveur.on("/", route_racine);
  serveur.on("/readings", route_readings);
  serveur.begin();

  mesurer();   // une premiere mesure tout de suite
}

void loop() {
  serveur.handleClient();

  // millis() deborde au bout de 49 jours. La soustraction en arithmetique
  // non signee reste correcte au passage - c'est la raison d'ecrire
  // "maintenant - derniere >= intervalle" et jamais
  // "maintenant >= derniere + intervalle", qui casse au debordement.
  uint32_t maintenant = millis();

  if (maintenant - derniere_mesure >= INTERVALLE_MS) {
    derniere_mesure = maintenant;
    mesurer();
  }

  // Reconnexion WiFi : une box qui redemarre ne doit pas condamner le
  // module jusqu'a la prochaine coupure de courant.
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.reconnect();
    delay(1000);
  }
}
