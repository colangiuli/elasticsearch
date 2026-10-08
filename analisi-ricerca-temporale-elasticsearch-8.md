# Ricerca temporale su tutti i campi data in Elasticsearch 8

Analisi del 1 ottobre 2026. Riferimento tecnico: documentazione ufficiale Elasticsearch 8.19; la minor effettivamente installata e il codice del fork di Kibana non sono stati forniti. Le soluzioni con OR di range e campo `date` multivalore usano capacità già presenti in Elasticsearch 8 e non richiedono un upgrade minor come prerequisito. Un eventuale upgrade resta una scelta separata da validare con il fork. Non sono stati interrogati cluster o dati del case, e gli esempi non sono stati eseguiti. I nomi di indici e campi negli esempi sono illustrativi.

**Perimetro della verifica del frontend:** le prime conclusioni sulla timeline riguardavano le capacità di Elasticsearch, non una verifica del pannello del fork. In seguito è stato esaminato il codice pubblico di Kibana 2 (`rashidkpc/kibana2`, branch `kibana-ruby`, commit `a7cdc7426205dccc782ee291dbf162f7a44bb1dc`). Questo codice storico non identifica le modifiche del vostro fork, il cui repository e commit non sono disponibili. La fattibilità dell'istogramma su array lato Elasticsearch è distinta dalla compatibilità già presente nel frontend; i dettagli verificati dell'upstream sono riportati nella sezione di integrazione.

**Scala dichiarata:** un case può comprendere fino a circa mille host aziendali, inclusi file server e domain controller, con gigabyte di eventi raccolti per host. Numero di documenti, distribuzione dei campi data, dimensioni degli indici e concorrenza non sono ancora misurati.

**Ciclo di vita dichiarato:** la funzione sarà disponibile soltanto per i nuovi case; non è richiesto normalizzare o riprocessare lo storico. I dati vengono eliminati alla chiusura del case. I case preesistenti mantengono il comportamento attuale fino alla chiusura.

**Organizzazione dichiarata degli indici:** un indice per ogni artifact source per ogni case; nessun indice contiene dati di case diversi. Un alias esistente include tutti gli indici del case e costituisce il punto di accesso per la ricerca globale. Il numero degli host contribuisce al volume degli indici delle source e non costituisce un ulteriore moltiplicatore del numero di indici.

**Raccomandazione per questa scala e questo ciclo di vita:** adottare come candidato principale un campo data multivalore indicizzato, popolato durante il parsing dal primo import di ogni nuovo case. Non servono migrazione, backfill o fallback di lettura dello storico. Confrontare le prestazioni con un OR di query `range` sui campi originali: l'OR resta un'alternativa valida e un riferimento di correttezza, ma non è una fase obbligatoria del rilascio. Il campo materializzato riduce il lavoro di ricerca su molti campi, aggiunge dati da indicizzare e non riduce automaticamente gli shard interrogati. Evitare runtime/script come filtro generale su tutto il case. Un indice separato per osservazione temporale va giustificato da un requisito di timeline per singolo evento: la moltiplicazione dei documenti può essere significativa.

## Applicazione alla struttura case × artifact source

Mantenere l'organizzazione esistente e aggiungere lo stesso campo temporale aggregato nei mapping delle source. Ogni source conserva il proprio schema e il parser conosce i campi da includere; nome, tipo e semantica del campo comune devono essere coerenti tra gli indici. La scelta di precisione descritta più avanti va risolta nello schema condiviso. Un component template può riutilizzare la definizione comune all'interno dei template specifici delle source; è un'opzione di implementazione, non richiede di accorpare gli indici. [Component templates](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/indices-component-template.html)

La ricerca su tutto il case usa una singola richiesta `_search` diretta all'alias già esistente, con una range sul campo comune. Il raggruppamento degli indici è quindi già risolto dall'architettura attuale: non occorre introdurre una seconda lista o un nuovo alias per la funzione. Gli esempi JSON successivi sono corpi di richieste dirette all'alias del case e non richiedono un campo `case_id` nel documento. L'alias raggruppa gli indici, mentre il mapping comune di `forensic_all_dates` va definito negli indici delle source dei nuovi case. [Ricerca su più indici](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-multiple-indices.html)

Esempio di destinazione, con nome illustrativo dell'alias del case:

```http
POST /case-123-evidence/_search
```

L'OR sui campi originali beneficia anch'esso di questa organizzazione: il catalogo può essere definito per source, con una struttura tendenzialmente più omogenea all'interno di ciascun indice. Ciò non garantisce assenza di cambi di schema tra versioni dell'artefatto, né elimina conflitti tra nomi di campi di source diverse. Il campo comune riduce questa complessità per il solo filtro temporale. Se una vista seleziona una source specifica, interrogare soltanto il relativo indice; la modalità globale deve invece coprire tutte le source pertinenti del case.

Per A source indicizzate in un case esistono A indici. Con p_i shard primari per indice, una ricerca globale ha fino a Σ p_i shard logici candidati, prima di eventuali esclusioni decise da Elasticsearch. Non moltiplicare questo conteggio per tutte le repliche: la ricerca viene instradata verso una copia di ciascuno shard coinvolto. Esempio illustrativo: 60 source con 2 primari ciascuna danno 120 shard candidati, non 60 × 1000 host. [Search shard routing](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-shard-routing.html)

Questa struttura consente di dimensionare diversamente gli indici più grandi e quelli piccoli. Non assumere che tutte le source richiedano lo stesso numero di primari: MFT, log e artefatti poco popolati possono avere volumi molto diversi. La chiusura del case deve rimuovere l'intero insieme dei suoi indici, inclusi eventuali indici derivati; non serve filtrare documenti di case diversi dentro uno stesso indice. Il nuovo campo aggregato non aumenta il numero degli indici.

## Il requisito da rendere esplicito

Il filtro richiesto seleziona un documento se **almeno uno dei suoi valori data è nell'intervallo**: `case autorizzato AND altri filtri AND EXISTS(data in [inizio, fine))`. Un record MFT con creazione in gennaio e modifica in settembre deve comparire nella ricerca di settembre anche quando `@timestamp` contiene la creazione di gennaio.

Questo restituisce documenti contenenti osservazioni temporali pertinenti. Non trasforma automaticamente ciascuna data in un evento distinto, né dimostra da solo cosa sia realmente avvenuto in quell'istante. L'artefatto ufficiale `Windows.NTFS.MFT` espone, per esempio, `Created`, `LastModified`, `LastRecordChange` e `LastAccess`, nelle varianti `0x10` e `0x30`. Offre anche filtri temporali al momento della raccolta: Elasticsearch non può recuperare record già esclusi dal collector o dal parser. [Artefatto ufficiale Velociraptor](https://docs.velociraptor.app/artifact_references/pages/windows.ntfs.mft/)

Per il requisito concordato, il catalogo comprende `date` e `date_nanos`, incluso `@timestamp`, ma esclude `acquisition_time` e gli eventuali multifield derivati da quel campo. La stessa data resta inclusa se compare anche in un campo di evidenza ammesso: l'esclusione riguarda la sorgente, non il valore temporale. Il catalogo selezionato deve essere visibile; non bisogna presentarlo come se coprisse letteralmente tutte le date del documento.

Un valore simile a una data ma mappato `keyword` o `long` non appartiene automaticamente al catalogo dei campi data. Il requisito applicativo «qualsiasi timestamp forense» può quindi richiedere anche correzioni del parser e dei mapping. JSON non ha un tipo data nativo: il significato è assegnato dal mapping; `date` ha precisione al millisecondo e converte gli istanti in UTC. [Tipo `date`](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/date.html)

## Confronto sintetico

Costi e prestazioni sono valutazioni architetturali, non benchmark. Le giornate sono ordini di grandezza per uno sviluppatore che conosce il prodotto, con schema relativamente stabile, test di integrazione e modifica essenziale della UI; escludono dimensionamento e collaudo del cluster a pieno carico ed eventuali riscritture estese del fork. Il lavoro sullo storico è fuori perimetro. Le stime applicative restano indicative: il risparmio rispetto a un rilascio con migrazione riguarda soprattutto elaborazioni retroattive, transizione e spazio temporaneo, già esclusi dalle stime iniziali. Con gigabyte per host, la validazione operativa resta significativa.

| Soluzione | Sviluppo indicativo | Introduzione nei nuovi case | Costo query atteso | Disco e ingestione | Limite principale |
|---|---:|---|---|---|---|
| 1. OR di `range` sui campi originali | Basso-medio; 3–8 giornate | Nuovo costruttore di query e catalogo | Buono con pochi/decine di campi; aumenta con campi, shard e concorrenza | Quasi invariato | Catalogo, conflitti di mapping e `nested` |
| 2. Campo multivalore indicizzato | Medio; 6–15 giornate | Mapping e parser aggiornati dal primo import | Un solo campo indicizzato; in genere più prevedibile | Aggiunge valori e lavoro di parsing/indicizzazione | Completezza del catalogo e provenienza dei match |
| 3. Runtime field `date` | Basso-medio; 2–6 giornate | Script nella query o nel mapping | Più CPU; sconsigliato come filtro generale alla scala descritta | Nessun nuovo valore persistito | Ricerche costose e precisione millisecondi |
| 4. Indice timeline separato | Alto; 15–30+ giornate | Generazione delle osservazioni a ogni import | Range, sort e istogrammi semplici | Moltiplica i documenti temporali | Nuovo modello, sincronizzazione e deduplicazione |

### 1. Costruire un OR di range sui campi originali

L'applicazione scopre i campi data del case e genera una clausola `range` per ciascuno, racchiusa in `bool.should` con `minimum_should_match: 1`. Il filtro temporale vive in `filter`, così non serve attribuire punteggi di rilevanza. Una query `range` identifica un campo; l'espansione «tutti i campi di tipo data» è responsabilità del codice che costruisce la query. Per date e limiti è preferibile la DSL esplicita rispetto a una stringa Lucene costruita dinamicamente. [Range query](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/query-dsl-range-query.html), [Query string e range](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/query-dsl-query-string-query.html)

Discovery di base:

```http
GET /case-123-evidence/_field_caps?fields=*&include_unmapped=true
GET /case-123-evidence/_mapping
```

`_field_caps` restituisce tipi, capacità e differenze tra indici; in 8.19 può filtrare anche tramite `types=date,date_nanos`. Usare la richiesta senza `types` e selezionare i risultati nell'applicazione evita di assumere quel parametro su ogni minor 8.x. L'API include anche i runtime field: distinguerli dai campi indicizzati evita aspettative di performance errate. `_mapping` completa il catalogo con gerarchia `nested`, formati e configurazione `index`/`doc_values`. Cache del catalogo per case e invalidazione dopo nuovi import o modifiche di schema. [Field capabilities](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-field-caps.html), [Get mapping](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/indices-get-mapping.html)

Esempio ridotto a tre campi, che nella versione reale vanno sostituiti con l'intero catalogo:

```json
{
  "query": {
    "bool": {
      "filter": [
        {
          "bool": {
            "minimum_should_match": 1,
            "should": [
              { "range": { "@timestamp": {
                "gte": "2026-09-20T00:00:00Z",
                "lt": "2026-09-21T00:00:00Z",
                "format": "strict_date_optional_time",
                "_name": "time_canonical"
              } } },
              { "range": { "Created0x10": {
                "gte": "2026-09-20T00:00:00Z",
                "lt": "2026-09-21T00:00:00Z",
                "format": "strict_date_optional_time",
                "_name": "time_created_0x10"
              } } },
              { "range": { "LastModified0x10": {
                "gte": "2026-09-20T00:00:00Z",
                "lt": "2026-09-21T00:00:00Z",
                "format": "strict_date_optional_time",
                "_name": "time_modified_0x10"
              } } }
            ]
          }
        }
      ]
    }
  }
}
```

`minimum_should_match: 1` va scritto esplicitamente: aggiungere filtri allo stesso `bool` può altrimenti rendere facoltative le clausole `should`. I nomi univoci delle clausole permettono di leggere `matched_queries` per hit e spiegare quali campi hanno soddisfatto il filtro; per il valore preciso si leggono poi i campi originali. Anche questa spiegazione ha un costo e va misurata su pagine di risultati realistiche. [Boolean query e named queries](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/query-dsl-bool-query.html)

**Benefici:** nessuna copia persistente, utilizzo dei campi esistenti; è facile aggiungere il filtro al costruttore di query del prodotto. È un buon riferimento di correttezza con cui confrontare le altre opzioni. Il vantaggio di poter interrogare lo storico senza migrazione non è determinante nel perimetro richiesto.

**Costi e criticità:** il lavoro cresce con il numero di campi interrogati, ma non significa necessariamente una scansione completa per campo: i campi indicizzati permettono a Elasticsearch di usare le proprie strutture di ricerca. Molti shard e molti campi possono rendere costosa anche una query con pochi risultati. In ES8 il limite delle clausole è calcolato dinamicamente e ha minimo 1024; il vecchio `indices.query.bool.max_clause_count` è deprecato e non ha effetto. Annidare altri `bool` non aggira il limite. [Search settings](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-settings.html)

Se lo stesso nome è `date` in un indice e `keyword`, numerico o `nested` in un altro, raggruppare gli indici compatibili e costruire query specifiche. Aggiungere un filtro `_index` dentro una grande query non va considerato una garanzia contro gli errori di parsing delle clausole su mapping incompatibili. Eventuali richieste separate richiedono merging, ordinamento e paginazione coerenti nel backend. Un campo mancante è diverso da un campo esistente con tipo incompatibile.

Per una data sotto `nested`, la range deve stare nella corrispondente query `nested`; una range al livello radice non basta. `inner_hits` può mostrare l'elemento che corrisponde. Nei mapping `object`, invece, Elasticsearch appiattisce gli array di oggetti: per la sola esistenza di qualsiasi data può bastare, ma non conserva automaticamente l'associazione «nome del campo, valore, elemento». [Nested query](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/query-dsl-nested-query.html), [Arrays](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/array.html)

### 2. Materializzare un campo data multivalore

Il parser aggiunge, per ogni documento, un campo come `forensic_all_dates` contenente i valori di tutti i campi data inclusi nel contratto. Elasticsearch non richiede un tipo array: un campo `date` può contenere più valori. Una singola `range` sul campo multivalore seleziona il documento quando almeno un valore rientra nell'intervallo. Il documento rimane uno. [Tipi e array](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/mapping-types.html)

Esempio del dato derivato:

```json
{
  "@timestamp": "2026-01-12T09:30:00Z",
  "Created0x10": "2026-01-12T09:30:00Z",
  "LastModified0x10": "2026-09-20T16:45:00Z",
  "forensic_all_dates": [
    "2026-01-12T09:30:00Z",
    "2026-09-20T16:45:00Z"
  ],
  "forensic_time_schema_version": "1"
}
```

La deduplicazione dei valori identici nell'array è lecita per il filtro esistenziale; la provenienza rimane nei campi originali. L'array derivato stesso non deve essere ricopiato nelle future estrazioni.

Mapping del nuovo campo e query temporale:

```json
{
  "properties": {
    "forensic_all_dates": {
      "type": "date",
      "format": "strict_date_optional_time||epoch_millis"
    },
    "forensic_time_schema_version": { "type": "keyword" }
  }
}
```

```json
{
  "query": {
    "bool": {
      "filter": [
        { "range": { "forensic_all_dates": {
          "gte": "2026-09-20T00:00:00Z",
          "lt": "2026-09-21T00:00:00Z"
        } } }
      ]
    }
  }
}
```

**Dove costruirlo:** preferibilmente nel parser che già conosce gli artefatti e normalizza i timestamp. Una ingest pipeline è alternativa valida, ma deve ricevere o codificare lo stesso catalogo: attraversare ricorsivamente il JSON e tentare di interpretare ogni stringa non equivale a conoscere i tipi del mapping. La scelta deve coprire anche gli artefatti nuovi e registrare i valori non convertibili.

**Variante `copy_to`:** i campi sorgente possono copiare il valore verso il campo aggregato. È una soluzione compatta quando i mapping sono controllati, ma copia il valore originario, non un istante già normalizzato, e non aggiunge l'array a `_source`. Il target deve accettare i formati e le unità di tutte le sorgenti: per esempio epoch seconds e epoch millis non vanno confusi. Non si propaga ricorsivamente e non è supportato per valori a oggetto come `date_range`. Per dati eterogenei e provenienza forense, il parser è normalmente più controllabile. [copy_to](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/copy-to.html)

**Benefici:** query piccola, minore dipendenza della ricerca dal numero di campi originali, semantica stabile per la UI. Le ricerche frequenti evitano di ricalcolare l'unione di tutti i timestamp.

**Costi:** con N documenti e k valori medi, si aggiunge un ordine di N×k valori indicizzati, oltre ai dati originali. Il costo su disco dipende da compressione, duplicati, doc values e dalla scelta di salvare anche l'array in `_source`; non è corretto stimare un incremento percentuale senza misura. Aumenta il lavoro di ingestione, mentre la latenza tende a essere più prevedibile. Non è previsto alcun backfill: il campo nasce insieme ai documenti dei nuovi case. Non è garantito che sia sempre più veloce dell'OR su pochi campi selettivi.

**Limiti:** l'array non dice da quale campo provenga il match. Una coppia di array paralleli «date» e «nomi» è fragile: l'accesso ai valori indicizzati non è un contratto di conservazione dell'ordine originale. Conservare i campi originali o una struttura di provenienza in `_source`; se occorre cercare congiuntamente coppie nome/data si può usare `nested`, accettandone i documenti interni e il costo. [Tipo nested](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/nested.html)

**Precisione:** un target `date` perde la precisione inferiore al millisecondo dei campi `date_nanos`. Un target `date_nanos` conserva maggiore precisione, ma ammette indicativamente date dal 1970 al 2262; non è un sostituto universale per tutti i valori `date`. Inoltre le aggregazioni su `date_nanos` restano a risoluzione millisecondi. Se servono sia date precedenti al 1970 sia precisione maggiore, mantenere due campi aggregati separati per tipo e un OR tra due range, oppure conservare la soluzione 1. [Date nanoseconds](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/date_nanos.html)

### 3. Un runtime field che emette tutte le date

Si definisce un campo virtuale `date` in `runtime_mappings` della richiesta, oppure nel mapping, e lo script emette i valori dei campi del catalogo. Lo script viene calcolato al momento della ricerca, quindi funziona anche sui documenti già presenti. I runtime field non modificano `_source` e si recuperano con `fields`. Le query che li usano sono considerate costose e vengono rifiutate se `search.allow_expensive_queries=false`. [Runtime fields](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/runtime.html)

Esempio limitato a campi `date` non nested, con doc values disponibili e mapping coerenti:

```json
{
  "runtime_mappings": {
    "forensic_all_dates_runtime": {
      "type": "date",
      "script": {
        "params": {
          "fields": ["@timestamp", "Created0x10", "LastModified0x10"]
        },
        "source": "for (def f : params.fields) { if (!doc.containsKey(f) || doc[f].size() == 0) continue; for (def v : doc[f]) { emit(v.toInstant().toEpochMilli()); } }"
      }
    }
  },
  "query": {
    "bool": {
      "filter": [
        { "range": { "forensic_all_dates_runtime": {
          "gte": "2026-09-20T00:00:00Z",
          "lt": "2026-09-21T00:00:00Z"
        } } }
      ]
    }
  }
}
```

Painless permette più chiamate `emit` per produrre un campo multivalore; non permette di emettere `null`. L'elenco dei campi è un parametro controllato dall'applicazione, non una stringa di script arbitraria fornita dall'utente. [Runtime fields context](https://www.elastic.co/guide/en/elasticsearch/painless/8.19/painless-runtime-fields-context.html)

**Benefici:** nessuna duplicazione persistente, facile modificare e provare il catalogo. È utile per un prototipo o per ricerche con pochi candidati. L'assenza di backfill non rappresenta un vantaggio competitivo nel perimetro richiesto, perché neppure le altre opzioni devono coprire lo storico.

**Costi e limiti:** lo script deve valutare i valori per i documenti candidati; concettualmente il lavoro cresce con candidati×valori da esaminare. Il costo reale dipende dal piano di esecuzione e dai filtri indicizzati preliminari. Nel vostro modello il case è già delimitato dagli indici: ulteriori restrizioni di host o artifact source possono aiutare, ma una ricerca globale nel case resta ampia. Reintrodurre il range su `@timestamp` per velocizzare falserebbe il risultato richiesto.

Il catalogo dei tipi runtime in 8.19 comprende `date`, ma non `date_nanos`: l'esempio converte esplicitamente a epoch millis e non conserva precisione sub-millisecondo. I doc values sono preferibili a `_source` per velocità; leggere e interpretare `_source` a ogni ricerca è più costoso. Campi nested richiedono un progetto dedicato, perché il documento radice non espone i loro doc values come normali campi. Non ignorare silenziosamente errori di script: un risultato incompleto deve essere riconoscibile. [Mapping dei runtime field](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/runtime-mapping-fields.html), [Nested query](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/query-dsl-nested-query.html)

### 4. Un indice timeline con un documento per timestamp

Il parser produce un indice derivato separato, anch'esso dedicato al singolo case. Ogni osservazione temporale ha il proprio `@timestamp`, il nome del campo sorgente, identificatori di host/artefatto e un riferimento stabile all'evidenza originale. Il `case_id` nell'esempio è metadato facoltativo, non serve a selezionare il case dentro un indice condiviso. L'indice originale rimane disponibile per leggere il record completo. Si tratta di una proposta di modello applicativo, non di una funzione automatica di Kibana.

```json
{
  "case_id": "123",
  "evidence_id": "evidence-789",
  "host_id": "host-42",
  "artifact": "Windows.NTFS.MFT",
  "@timestamp": "2026-09-20T16:45:00Z",
  "timestamp_field": "LastModified0x10",
  "source_reference": "collection-456/mft.jsonl:1208",
  "parser_version": "1"
}
```

La ricerca è una normale range sul `@timestamp` di questa vista; la UI può ordinare le righe cronologicamente e mostrare la provenienza senza ricostruirla dai campi originali. Il backend recupera il documento originale quando l'analista apre il dettaglio. È necessario decidere esplicitamente se mostrare osservazioni temporali o documenti distinti.

**Benefici:** migliore base per una timeline forense completa, ordinamento per il timestamp pertinente, analisi per tipo di data e conteggi di osservazioni comprensibili. Consente di trattare separatamente date di attività, acquisizione e ingestione.

**Costi:** con k osservazioni per evidenza, si generano circa N×k documenti timeline; questo non equivale necessariamente a k volte lo spazio dell'indice originale se i documenti derivati sono piccoli. Servono gestione di import ripetuti, ID deterministici, aggiornamenti/cancellazioni, autorizzazioni coerenti, versionamento parser e controllo delle elaborazioni parziali. Durante una reimportazione evitare finestre in cui il record sorgente e la timeline appartengono a versioni diverse.

**Criticità:** un record MFT può produrre molte righe. Due timestamp identici in campi diversi possono essere due osservazioni valide; la copia tecnica in `@timestamp` non va interpretata automaticamente come un evento indipendente. Filtri su campi presenti solo nell'evidenza originale non sono applicabili all'indice timeline senza copiarli o gestire una correlazione applicativa. Una vista che restituisce solo evidenze distinte richiede deduplicazione e paginazione dedicate.

## Integrazione nel fork di Kibana

Non è possibile stimare dal solo nome «fork di Kibana 2» quali componenti siano riutilizzabili. Le soluzioni qui descritte usano API Elasticsearch; non presuppongono supporto del fork a data view moderne, runtime field nell'editor o Lens.

Nel codice pubblico storico verificato, `DateHistogram` accetta il nome del campo, con default `@timestamp`, ma costruisce una richiesta `facets.count.date_histogram`, non l'aggregazione `aggs` degli esempi ES8. La classe base `Query` applica comunque un filtro temporale su `@timestamp` scritto direttamente nel codice: cambiare soltanto il campo dell'istogramma non cambia quel filtro. [Query e DateHistogram di Kibana 2](https://github.com/rashidkpc/kibana2/blob/a7cdc7426205dccc782ee291dbf162f7a44bb1dc/lib/query.rb#L39-L58), [costruzione dell'istogramma](https://github.com/rashidkpc/kibana2/blob/a7cdc7426205dccc782ee291dbf162f7a44bb1dc/lib/query.rb#L151-L163)

L'endpoint del grafico chiama `DateHistogram` senza passare un campo alternativo. Il frontend legge `facets[mode].entries` e costruisce punti tempo/conteggio per Flot: il renderer usa i bucket ricevuti, non analizza direttamente gli array dei documenti. La selezione di un intervallo nel grafico aggiorna lo stato temporale e ricarica i risultati; non va assunto che tutte le interazioni descritte come click sulle barre siano già implementate nel fork. [Endpoint graph](https://github.com/rashidkpc/kibana2/blob/a7cdc7426205dccc782ee291dbf162f7a44bb1dc/lib/kibana-app.rb#L74-L89), [lettura della risposta](https://github.com/rashidkpc/kibana2/blob/a7cdc7426205dccc782ee291dbf162f7a44bb1dc/public/lib/js/ajax.js#L205-L250), [rendering e selezione temporale](https://github.com/rashidkpc/kibana2/blob/a7cdc7426205dccc782ee291dbf162f7a44bb1dc/public/lib/js/ajax.js#L1313-L1351)

Questi riscontri confermano che il generatore della query, il filtro temporale e l'adattamento della risposta sono i punti da verificare. Non dimostrano che il vostro fork usi ancora queste API o abbia gli stessi vincoli: il collegamento dichiarato con ES8 può includere modifiche o uno strato di compatibilità non ispezionato. La modifica esatta e il suo costo richiedono il codice del fork o almeno la richiesta e la risposta effettive del suo grafico. Il rendering di un istogramma su array resta una possibilità architetturale, non una funzionalità del fork già collaudata.

**Il conteggio storico non va confuso con quello ES8 proposto.** Nel codice di Elasticsearch 0.90.13, la vecchia facet temporale richiamata da quella generazione di Kibana cicla i valori di ciascun documento e incrementa il bucket per ogni valore, senza la deduplicazione per documento/bucket del moderno `date_histogram`. Le spiegazioni successive di `doc_count` si riferiscono alle aggregazioni ES8; non descrivono automaticamente il vecchio pannello o una traduzione custom della sua API. Questo rende necessaria la verifica della richiesta/risposta effettiva del fork anche per confermare il significato delle barre. [Iterazione dei valori nelle facets storiche](https://github.com/elastic/elasticsearch/blob/v0.90.13/src/main/java/org/elasticsearch/search/facet/LongFacetAggregatorBase.java), [conteggio della date histogram facet](https://github.com/elastic/elasticsearch/blob/v0.90.13/src/main/java/org/elasticsearch/search/facet/datehistogram/CountDateHistogramFacetExecutor.java)

Il controllo UI consigliato è un intervallo unico con una scelta esplicita tra «timestamp principale», «tutti i campi data» e, se utile, «date evento selezionate». Mostrare campo e valore che hanno determinato il risultato. Preservare questo stato in ricerche salvate, URL ed esportazioni.

Per integrare il filtro, distinguere la modifica necessaria dal perimetro già gestito:

1. **Sostituire il filtro temporale precedente quando è attiva la nuova modalità.** Se si aggiunge l'OR lasciando `@timestamp in intervallo` in AND, si continua a perdere esattamente il record dell'esempio iniziale.
2. **Riutilizzare l'alias del case: punto già coperto.** L'alias esistente comprende tutti gli indici del case ed è la destinazione della ricerca e della discovery. Non serve sviluppare un nuovo meccanismo di selezione delle source. Il nuovo filtro deve utilizzare questo stesso accesso globale; l'intervallo viene applicato al campo temporale scelto nel corpo della query.

Il backend indirizza discovery e ricerca all'alias del case autorizzato. Poiché gli indici non sono condivisi tra case, un filtro `case_id` nel corpo della query è superfluo per la selezione del case.

## Ricerca, ordinamento e grafici hanno semantiche diverse

**Una timeline a barre può basarsi direttamente sull'array indicizzato.** La query all'alias del case può usare `date_histogram` su `forensic_all_dates`; non è necessario creare un indice con un documento per timestamp per ottenere questo grafico. L'etichetta corretta è «documenti con almeno un timestamp nel periodo della barra». Il supporto è nel motore Elasticsearch; il pannello del fork deve inviare l'aggregazione sul nuovo campo e interpretarne correttamente i risultati.

Esempio, con tutte le ore nello stesso giorno e fuso:

| Documento | Date contenute |
|---|---|
| A | 09:10, 10:10, 10:20 |
| B | 10:40, 11:05 |

| Barra oraria | Documenti (`doc_count`) | Timestamp presenti, per confronto |
|---|---:|---:|
| 09:00–10:00 | 1 (A) | 1 |
| 10:00–11:00 | 2 (A e B) | 3 |
| 11:00–12:00 | 1 (B) | 1 |

L'istogramma standard restituisce la colonna dei documenti. Le due date di A nella stessa ora contribuiscono una sola volta a quel bucket. La somma delle barre è 4, ma i documenti unici sono 2: un documento può appartenere a più periodi. Questo comportamento è appropriato per esplorare le evidenze per intervallo. Cliccando la barra 10:00–11:00, la tabella interrogata con la relativa range mostra A e B una volta ciascuno; il dettaglio può mostrare le tre date corrispondenti e i loro campi originali. [Implementazione ES 8.19 del date histogram](https://github.com/elastic/elasticsearch/blob/v8.19.0/server/src/main/java/org/elasticsearch/search/aggregations/bucket/histogram/DateHistogramAggregator.java)

Il nucleo dell'aggregazione è il seguente; il corpo completo deve comprendere gli altri filtri, la range della finestra selezionata e i limiti del grafico:

```json
{
  "aggs": {
    "timeline": {
      "date_histogram": {
        "field": "forensic_all_dates",
        "fixed_interval": "1h",
        "min_doc_count": 0
      }
    }
  }
}
```

L'OR e l'array restituiscono ogni documento una volta; ordinare per il vecchio `@timestamp` mostra ancora il timestamp canonico. Anche il sort su un campo multivalore sceglie un valore tramite una modalità, come minimo o massimo: non sceglie automaticamente il timestamp che ha fatto match nel range. Per ordinare per «prima data corrispondente» serve una derivazione specifica o la vista timeline. Riordinare soltanto la pagina nel browser non produce un ordinamento globale corretto. [Sort su campi multivalore](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/sort-search-results.html)

Analogamente, applicare un range all'array seleziona documenti, non elimina gli altri valori dell'array. Un istogramma su quell'array può includere bucket esterni al periodo; `extended_bounds` non è un filtro. `hard_bounds` limita i bucket, ma non filtra esattamente i singoli valori nei bucket di confine, né risolve provenienza e conteggio degli eventi. Un documento può contribuire a più bucket, mentre più date dello stesso documento nello stesso bucket non devono essere lette come altrettanti documenti distinti. Per grafici dedicati va definito se si vogliono contare evidenze o osservazioni temporali. [Date histogram](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-aggregations-bucket-datehistogram-aggregation.html)

**Finestre non allineate alle barre:** per una finestra 09:45–11:15 e barre orarie, un documento con date 09:30 e 10:10 è selezionato correttamente dalla query, ma può contribuire impropriamente anche alla barra iniziale 09:00. Per conteggi esatti, una soluzione proposta consiste nel mantenere l'istogramma per i bucket interni e calcolare i due bucket parziali mediante range indicizzate 09:45–10:00 e 11:00–11:15 in aggregazioni `filter`/`filters`, condividendo gli stessi filtri globali. Sostituire i conteggi di confine anche quando risultano zero; se la finestra ricade in un solo bucket basta una correzione. La soluzione è dedotta dalla semantica delle aggregazioni e va collaudata sul fork. Non richiede runtime field o una scansione script dell'intero case. Anche eventuali sottoaggregazioni nei bucket parziali richiedono analoga gestione. [Filter aggregation](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-aggregations-bucket-filter-aggregation.html), [Filters aggregation](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-aggregations-bucket-filters-aggregation.html)

I `hard_bounds` devono coprire le chiavi arrotondate dei bucket da visualizzare, compreso il primo bucket parziale; `extended_bounds` può richiedere i bucket vuoti. Confini, timezone e offset devono usare lo stesso arrotondamento. Mantenere un numero controllato di barre adattando l'intervallo allo zoom. Elasticsearch esegue l'aggregazione e il frontend riceve i bucket, senza scaricare tutti i documenti. L'array aumenta comunque il numero di valori da aggregare rispetto a un timestamp singolo: misurare anche il costo del grafico, separatamente da quello della prima pagina di risultati.

Non sostituire l'array di istanti con una coppia min/max e una semplice intersezione di intervalli: un record con date solo in gennaio e dicembre diventerebbe falsamente pertinente a giugno. Min/max può essere al più un prefiltro, seguito dalla verifica sugli istanti reali.

Anche i due estremi devono stare nella **stessa** clausola `range`, come negli esempi. Due clausole separate `gte` e `lt` in AND possono essere soddisfatte da due valori diversi di un campo multivalore: un valore prima e uno dopo il periodo non devono diventare un match. Questa conseguenza della costruzione della query per valori multivalore va verificata esplicitamente nel collaudo del costruttore di query. [Implementazione delle date ES 8.19](https://github.com/elastic/elasticsearch/blob/v8.19.0/server/src/main/java/org/elasticsearch/index/mapper/DateFieldMapper.java), [Range sui valori LongPoint in Lucene](https://lucene.apache.org/core/9_12_2/core/org/apache/lucene/document/LongPoint.html)

## Rilascio sui nuovi case e verifiche necessarie

Creare un mapping/template versionato con il campo aggregato e applicarlo ai nuovi case prima del primo import. Registrare nel case la versione dello schema e la disponibilità della ricerca su tutte le date. Il parser deve produrre `forensic_all_dates` per ogni caricamento nel case, compresi gli import successivi e gli artefatti aggiunti durante l'indagine. La UI abilita il filtro in base alla capacità del case, senza inferirla soltanto dall'età di un indice o dalla presenza occasionale del campo.

La versione assegnata al case deve governare tutti i suoi indici, anche quelli creati in seguito. Un indice creato dopo il rilascio per un case precedente non deve abilitare accidentalmente la nuova modalità su una parte dei dati. I case preesistenti continuano con la ricerca attuale fino alla chiusura. Questo piano non richiede `_reindex`, `_update_by_query`, rilettura degli ZIP o ricerca mista tra schema precedente e nuovo.

Usare una fonte comune per definire i campi data dei mapping e quelli inclusi nell'array, evitando due elenchi indipendenti. Versionare schema/parser e registrare i campi non gestiti. L'aggiunta di un artefatto deve prevedere le sue date prima di indicizzarne il primo documento. Se una correzione futura cambia l'interpretazione di dati già importati in un case aperto, non dichiarare retroattivamente completa la copertura: la versione applicata e le eventuali lacune devono restare visibili. Questa gestione delle anomalie non introduce una migrazione dello storico nel rilascio attuale.

Per l'opzione 1, registrare esplicitamente gli indici o campi esclusi per accessi, mapping o dati mancanti; per l'opzione 3 aggiungere il perimetro di precisione e scripting. Per l'opzione 4 generare le osservazioni dal primo import e gestire la coerenza con il documento originale. Per tutte le opzioni controllare timeout, shard falliti ed errori d'importazione: un risultato parziale non deve apparire completo.

Il campo aggregato appartiene allo stesso documento dell'evidenza: la cancellazione prevista alla chiusura del case rimuove anche il dato derivato. L'opzione 4 richiede invece di includere esplicitamente gli indici timeline nello stesso ciclo di vita. Dimensionare storage e capacità sul picco di case contemporaneamente aperti e sul loro volume; la cancellazione alla chiusura limita l'accumulo nel tempo, ma non il picco necessario durante le indagini.

Se il mapping usa `ignore_malformed`, un valore non valido può essere conservato nel documento ma escluso dall'indicizzazione; `_ignored` registra i nomi dei campi ignorati e aiuta a misurare questo scarto. Il catalogo dei campi non è quindi una prova che ogni valore sorgente sia ricercabile. [Campo `_ignored`](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/mapping-ignored-field.html)

Il collaudo minimo dovrebbe includere:

- Timestamp canonico fuori intervallo e almeno un'altra data dentro; caso inverso; nessuna data dentro; date duplicate.
- Un array con valori solo prima e dopo l'intervallo, senza valori interni: deve restituire zero match.
- Campi assenti, null, valori non validi, nomi uguali con mapping incompatibile, campi nested, artefatti nuovi.
- Confini esatti con `[inizio, fine)`, UTC e conversioni da `Europe/Rome`, cambio dell'ora legale, precisione millisecondi/nanosecondi e date anomale.
- Documento con timestamp canonico fuori intervallo; ricerca su tutte le source, incluse quelle aggiunte dopo la creazione del case; nessun indice di altri case coinvolto.
- Verifica di campo e valore mostrati per il match, conteggi distinti rispetto a eventi e paginazione oltre la prima pagina.
- Istogramma su array: due timestamp nello stesso bucket contano un documento; lo stesso documento in due bucket contribuisce a entrambi; un valore esterno alla finestra non deve alterare il conteggio di un bucket parziale.

Per «tutti i documenti» occorre distinguere la selezione completa dal solo elenco della prima pagina. L'esportazione può usare `search_after` con PIT e ordinamento stabile; una sola risposta di ricerca non consegna automaticamente tutti i match. [Paginazione Elasticsearch](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/paginate-search-results.html)

Misurare sul corpus reale latenza p50/p95, CPU/heap, timeout e shard falliti, throughput e durata d'importazione, e dimensione dell'indice. Confrontare più ampiezze di intervallo, numero di campi data, numero di shard e utenti concorrenti, a cache fredda e calda. Questi dati determinano se il campo materializzato ripaga il costo operativo rispetto all'OR; non esiste una soglia universale di documenti.

## Effetto della scala: mille host e gigabyte per host

Per sola aritmetica, 1–10 GB di dati per ciascuno di mille host significano 1–10 TB di dati raccolti. Questo esempio non stima il vostro volume effettivo né lo spazio Elasticsearch: ZIP compressi, JSONL estratti, campi effettivamente indicizzati, indici primari e repliche sono grandezze diverse. La misura utile parte da documenti e byte effettivamente caricati.

Servono almeno N (documenti per case), k (valori data medi e distribuzione per artefatto), F (campi data distinti), S (shard interrogati) e C (ricerche concorrenti). Non basta il numero degli host. Un file server con molta MFT e un domain controller con molti log possono avere costi e selettività molto diversi; il campione deve includere entrambi e gli host più pesanti.

Un esempio puramente illustrativo: con un milione di documenti per host, mille host producono un miliardo di documenti. Se il campo derivato contiene in media sei valori, si aggiungono circa sei miliardi di valori data mantenendo un miliardo di documenti. Se invece la proiezione timeline genera in media sei osservazioni, produce sei miliardi di documenti aggiuntivi. I due moltiplicatori possono differire: l'array può deduplicare istanti identici, la timeline deve preservare le osservazioni con provenienze diverse. Non stimare i byte reali moltiplicando semplicemente per otto: strutture di indice, doc values, sorgenti, compressione e repliche contano.

Il campo aggregato riduce il numero di campi interrogati, **non** il numero di shard del case. L'OR sfrutta comunque gli indici dei campi data: non equivale a uno script che visita N×F valori. Viceversa, il runtime può richiedere calcoli su moltissimi candidati quando l'unico filtro selettivo è quello temporale calcolato. La preferenza per l'opzione 2 è quindi una scelta architetturale da misurare, non una promessa di un fattore di accelerazione. Elastic raccomanda di ridurre i campi interrogati quando utile e mostra `copy_to` come tecnica; il beneficio specifico per le vostre date deve essere verificato. [Ottimizzare la ricerca](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/tune-for-search-speed.html)

La disposizione degli shard va verificata insieme alle query mantenendo come base gli indici per case e artifact source già esistenti. Misurare numero e dimensione delle source per case, primari per source e totale sugli altri case aperti. Un numero eccessivo di shard piccoli aumenta l'overhead; pochi shard molto grandi possono limitare ricerca e recupero. La proposta del campo aggregato non richiede nuovi indici né un cambio di partizionamento. Elastic documenta l'overhead di indici e shard e raccomanda benchmark con dati, hardware, ricerche e ingestione rappresentativi. [Dimensionare gli shard](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/size-your-shards.html)

Per il benchmark, confrontare OR e campo materializzato sullo stesso corpus e a parità di risorse. Separare il tempo per la prima pagina, il conteggio esatto, l'istogramma e l'esportazione completa: sono carichi diversi. Provare finestre brevi e ampie, uno e tutti gli host, più analisti concorrenti e importazione contemporanea, a cache calda e fredda. Misurare p95/p99, CPU, heap/GC, I/O e cache filesystem, code/rifiuti di ricerca, byte primari e throughput d'importazione. Una prova su un solo host non dimostra la latenza di una ricerca distribuita su tutto il case.

Il rilascio sui soli nuovi case elimina lo spazio temporaneo per una migrazione e il carico di riscrittura del corpus esistente. Restano il costo aggiuntivo di ogni nuovo import e lo spazio dell'array per la durata del case. L'eliminazione alla chiusura si applica anche agli eventuali indici timeline; l'età di un timestamp forense non è di per sé un criterio per anticipare la cancellazione delle evidenze di un case aperto.

## Decisione proposta

1. Definire il catalogo dei campi per artifact source e il campo comune nei relativi mapping dei nuovi case; realizzare **l'opzione 2** nel parser come soluzione candidata dal primo import. Confrontarla con **l'opzione 1** nel benchmark, senza richiedere un rilascio preliminare dell'OR agli utenti.
2. È la scelta preferita per il nuovo filtro generale alla scala dichiarata, se il guadagno di latenza/concorrenza ripaga l'incremento di storage e ingestione. Se l'OR soddisfa gli obiettivi e l'array non offre un vantaggio concreto, sceglierlo rimane corretto.
3. Attivare la funzione solo sui nuovi case con schema compatibile, catalogo versionato e spiegazione del match. Nessuna normalizzazione dello storico. Preservare il collegamento tra valori derivati ed evidenze secondo il ciclo di vita del case.
4. Riservare **l'opzione 3** a prove e ricerche con un insieme già molto ristretto di candidati. Evitarla come fallback generale automatico su grandi case.
5. Scegliere **l'opzione 4** solo se serve una vista con una riga per osservazione, ordinamento e conteggi degli eventi. Una proiezione limitata a certi artefatti può contenerne il costo, ma va presentata come timeline parziale; non sostituisce la ricerca completa su tutte le date.
