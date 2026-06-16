# RESTGen pipeline

Uporządkowana wersja parserów i generatorów dla pipeline'u:

- `CM` – Class Model
- `PM` – Persistence Model
- `BIM` – Behavior Interface Model
- `BM` – Behavior Model

Stare nazwy `cfm/pfm/bifm/bfm` zostały przeniesione do `restgen/legacy/`. Nowy kod używa nazw `cm/pm/bim/bm`.

## Struktura

```text
restgen/
  common/
  parsers/
    cm_pm_parser.py        # CM + PM -> cm_pm_ir
    bim_parser.py          # BIM -> bim_ir
    bm_parser.py           # BM -> bm_ir
    structural_parser.py   # CM + PM + BIM -> structural_ir
  ir/
    merge.py               # structural_ir + bm_ir -> rest_ir
  generators/
    openapi_generator.py   # structural_ir -> openapi.yaml
    spring_backend_generator.py # rest_ir + OpenAPI interface -> Spring Boot backend
  pipeline.py              # jeden entrypoint CLI
  legacy/                  # stare pliki wejściowe zachowane pomocniczo
```

## Najprostszy pipeline

```bash
python -m restgen.pipeline all \
  --cm cfm-xmi.xml \
  --pm pfm-xmi.xml \
  --bim bifm-xmi.xml \
  --bm bfm-xmi.xml \
  --out out \
  --interface-dir generated-backend-interface \
  --force
```

To wygeneruje:

```text
out/structural_ir.json
out/bm_ir.json
out/rest_ir.json
out/openapi.yaml
out/generated-backend/
```

`--interface-dir` powinien wskazywać na wynik OpenAPI Generatora z `interfaceOnly=true`.

## Wariant z OpenAPI Generator CLI

Jeżeli masz lokalnie `openapi-generator-cli`, możesz pozwolić pipeline'owi odpalić go samodzielnie:

```bash
python -m restgen.pipeline all \
  --cm cfm-xmi.xml \
  --pm pfm-xmi.xml \
  --bim bifm-xmi.xml \
  --bm bfm-xmi.xml \
  --out out \
  --run-openapi-generator \
  --force
```

## Etapowo

### 1. Parsowanie

```bash
python -m restgen.pipeline parse \
  --cm cfm-xmi.xml \
  --pm pfm-xmi.xml \
  --bim bifm-xmi.xml \
  --bm bfm-xmi.xml \
  --out out
```

### 2. OpenAPI

```bash
python -m restgen.pipeline openapi \
  --structural-ir out/structural_ir.json \
  --out out/openapi.yaml
```

### 3. OpenAPI Generator

```bash
openapi-generator-cli generate \
  -i out/openapi.yaml \
  -g spring \
  -o out/generated-backend-interface \
  --additional-properties=interfaceOnly=true,useSpringBoot3=true,useTags=true,basePackage=org.openapitools
```

### 4. Backend

```bash
python -m restgen.pipeline backend \
  --rest-ir out/rest_ir.json \
  --interface-dir out/generated-backend-interface \
  --out out/generated-backend \
  --force
```

## Default values

Parser `cm_pm_parser.py` został poprawiony tak, żeby zbierać domyślne wartości atrybutów z XMI/EA, np. z:

```text
active: Boolean = true
```

W IR pole powinno dostać:

```json
"defaultValue": "true"
```

Generator backendu może wtedy wygenerować np.:

```java
private Boolean active = true;
```

oraz użyć tej wartości w mapperze, jeżeli pole istnieje w encji, ale nie istnieje w DTO wejściowym.

## Test backendu

```bash
cd out/generated-backend
mvn clean compile
mvn spring-boot:run
```

Potem test endpointu, np.:

```bash
curl -i -X POST http://localhost:8080/users \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "secret",
    "roles": ["USER"],
    "age": 20,
    "address": {
      "street": "Main",
      "city": "Wroclaw",
      "geoLocation": {
        "latitude": 51.1079,
        "longitude": 17.0385
      }
    }
  }'
```
