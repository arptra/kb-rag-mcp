export interface GraphConceptInfo {
  label: string;
  meaning: string;
  source: string;
  example: string;
}

export interface GraphEdgeInfo extends GraphConceptInfo {
  direction: string;
}

export const NODE_TYPE_ORDER = [
  "Repository",
  "Service",
  "BusinessOperation",
  "BusinessRule",
  "EntryPoint",
  "ExitPoint",
  "CodeSymbol",
  "DomainEntity",
  "Table",
  "Column",
  "Event",
  "ExternalSystem",
] as const;

export const NODE_TYPE_INFO: Record<string, GraphConceptInfo> = {
  Repository: {
    label: "Репозиторий",
    meaning: "Подключённый Git-репозиторий, внутри которого найдены один или несколько сервисов.",
    source: "Каталог подключённых repository и commit, использованный при последнем анализе.",
    example: "Git-репозиторий payments-platform, в котором найдены payments-api и payments-worker.",
  },
  Service: {
    label: "Сервис",
    meaning: "Граница развёртываемого сервиса или модуля, к которой относятся код и интерфейсы.",
    source: "gigacode-graph.json, module layout, spring.application.name или build descriptor; неоднозначные модули помечаются в замечаниях.",
    example: "spring.application.name=orders-service — все его controller, методы и таблицы получают один service_id.",
  },
  BusinessOperation: {
    label: "Бизнес-операция",
    meaning: "Операция, запускаемая входным HTTP/Kafka/Scheduled-интерфейсом. Название пока строится из имени handler-метода.",
    source: "Аннотированный controller/listener/scheduled method; это детерминированная семантика до обогащения GigaCode.",
    example: "Операция «Создать заказ», построенная от handler OrderController.createOrder().",
  },
  BusinessRule: {
    label: "Бизнес-правило",
    meaning: "Условие, которое влияет на выполнение операции, например проверка в if или выбрасывание исключения.",
    source: "Статически найденное условие в методах, достижимых из входной операции; сырая эвристика с evidence file:line.",
    example: "if (balance < total) throw InsufficientFundsException — правило «денег должно хватать».",
  },
  EntryPoint: {
    label: "Входная точка",
    meaning: "Интерфейс, через который выполнение входит в сервис: HTTP endpoint, Kafka listener или scheduled job.",
    source: "Spring mapping-аннотации, @KafkaListener и @Scheduled.",
    example: "@PostMapping(\"/orders\") или @KafkaListener(topics=\"orders.created\").",
  },
  ExitPoint: {
    label: "Исходящий вызов",
    meaning: "Место, где сервис вызывает другой сервис/систему или публикует событие.",
    source: "Feign/HttpExchange, поддерживаемые HTTP-клиенты и literal URL, Kafka producer или @SendTo.",
    example: "paymentClient.reserve(orderId) или kafkaTemplate.send(\"orders.created\", event).",
  },
  CodeSymbol: {
    label: "Метод кода",
    meaning: "Java/Kotlin метод или символ, участвующий во входной операции и статической цепочке вызовов.",
    source: "Tree-sitter разбор релевантных классов и bounded call tracing по полям и вызовам методов.",
    example: "OrderController.createOrder() → OrderService.createOrder() → OrderRepository.save(). Это узлы кода; стрелки CALLS между ними — связи внутри цепочки выполнения.",
  },
  DomainEntity: {
    label: "Доменная сущность",
    meaning: "Класс предметной модели, сопоставленный с таблицей хранения.",
    source: "JPA @Entity и @Table.",
    example: "@Entity class Order — объект предметной области «Заказ».",
  },
  Table: {
    label: "Таблица",
    meaning: "Таблица данных, которой владеет или которую использует сервис.",
    source: "JPA @Table либо SQL/Liquibase/Flyway migration.",
    example: "@Table(name=\"orders\") или CREATE TABLE orders в Liquibase/Flyway.",
  },
  Column: {
    label: "Колонка",
    meaning: "Поле таблицы, связанное с полем entity.",
    source: "JPA @Column/@JoinColumn; без явного имени используется имя поля и понижается уверенность.",
    example: "Order.customerId → orders.customer_id.",
  },
  Event: {
    label: "Событие / топик",
    meaning: "Kafka topic, через который сервисы публикуют и потребляют события.",
    source: "@KafkaListener, KafkaTemplate/StreamBridge и @SendTo с разрешением значений из конфигурации.",
    example: "orders.created.v1: order-service публикует, notification-service потребляет.",
  },
  ExternalSystem: {
    label: "Внешняя система",
    meaning: "Цель исходящего вызова, которую не удалось однозначно сопоставить с известным сервисом.",
    source: "target hint, URL или имя клиента; это может быть настоящая внешняя система либо ещё не разрешённый внутренний сервис.",
    example: "https://api.stripe.com или paymentClient, если соответствующий внутренний payment-service не найден.",
  },
};

export const EDGE_TYPE_INFO: Record<string, GraphEdgeInfo> = {
  CONTAINS: { label: "Содержит", direction: "Repository → Service", meaning: "Репозиторий содержит найденную границу сервиса.", source: "Repository/module layout.", example: "payments-platform → payments-api." },
  IMPLEMENTS: { label: "Реализует", direction: "Service → BusinessOperation", meaning: "Сервис реализует входную бизнес-операцию.", source: "Handler, из которого создана операция.", example: "orders-service → «Создать заказ»." },
  TRIGGERED_BY: { label: "Запускается через", direction: "BusinessOperation → EntryPoint", meaning: "Показывает входной интерфейс, запускающий операцию.", source: "HTTP/Kafka/Scheduled annotation.", example: "«Создать заказ» → POST /orders." },
  HANDLED_BY: { label: "Обрабатывается методом", direction: "EntryPoint → CodeSymbol", meaning: "Входная точка передаёт выполнение конкретному handler-методу.", source: "Метод с входной аннотацией.", example: "POST /orders → OrderController.createOrder()." },
  CALLS: { label: "Вызывает", direction: "CodeSymbol/BusinessOperation → CodeSymbol", meaning: "Один метод вызывает другой в пределах статически прослеженной цепочки. Если service_id одинаковый — это внутренняя связь; если разный — межсервисная.", source: "Tree-sitter call tracing; обычно MEDIUM.", example: "OrderController.createOrder() → OrderService.createOrder()." },
  EXITS_VIA: { label: "Выходит через", direction: "Service/BusinessOperation → ExitPoint", meaning: "Операция или сервис выполняет исходящий вызов.", source: "Outbound HTTP/Kafka evidence.", example: "«Создать заказ» → paymentClient.reserve()." },
  DEPENDS_ON: { label: "Зависит от", direction: "Service/ExitPoint → Service/ExternalSystem", meaning: "Исходящий контракт сопоставлен с сервисом или оставлен внешней/нераспознанной целью. В режиме «Сервисы» параллельные операции агрегируются по протоколу.", source: "Alias + HTTP contract или совпадение Kafka producer/consumer; GigaCode может подтвердить, отклонить или переназначить цель.", example: "orders-service → payments-service по POST /payments/reserve." },
  GUARDED_BY: { label: "Ограничена правилом", direction: "BusinessOperation → BusinessRule", meaning: "Выполнение операции зависит от найденного условия.", source: "if-condition в статически достижимом методе.", example: "«Оплатить заказ» → balance >= total." },
  DECLARES_ENTITY: { label: "Объявляет сущность", direction: "Service → DomainEntity", meaning: "Сервис объявляет JPA entity.", source: "@Entity.", example: "orders-service → Order." },
  MANAGES_SCHEMA: { label: "Управляет схемой", direction: "Service → Table", meaning: "Сервис создаёт или мигрирует таблицу.", source: "SQL/Liquibase/Flyway migration.", example: "orders-service → orders." },
  MAPS_TO: { label: "Сопоставлена с", direction: "DomainEntity → Table", meaning: "Entity отображается в таблицу.", source: "@Entity/@Table.", example: "Order → orders." },
  HAS_COLUMN: { label: "Содержит колонку", direction: "Table → Column", meaning: "Таблица содержит колонку entity.", source: "@Column/@JoinColumn.", example: "orders → customer_id." },
  READS: { label: "Читает", direction: "BusinessOperation → Table", meaning: "Операция читает данные из таблицы.", source: "Префикс вызова repository-метода; статическая эвристика.", example: "«Получить заказ» → orders через findById()." },
  WRITES: { label: "Записывает", direction: "BusinessOperation → Table", meaning: "Операция изменяет данные таблицы.", source: "Префикс вызова repository-метода; статическая эвристика.", example: "«Создать заказ» → orders через save()." },
  PUBLISHES: { label: "Публикует", direction: "Service/BusinessOperation/ExitPoint → Event", meaning: "Сервис или операция публикует сообщение в топик.", source: "KafkaTemplate/StreamBridge/@SendTo.", example: "orders-service → orders.created.v1." },
  CONSUMES: { label: "Потребляет", direction: "Service/BusinessOperation → Event", meaning: "Сервис или операция получает сообщения из топика.", source: "@KafkaListener.", example: "notification-service → orders.created.v1." },
};

export const CONFIDENCE_ORDER = ["DECLARED", "HIGH", "MEDIUM", "LOW", "UNRESOLVED"] as const;

export const CONFIDENCE_INFO: Record<string, GraphConceptInfo> = {
  DECLARED: { label: "Декларативно", meaning: "Связь явно задана конфигурацией/manifest, а не угадана анализатором.", source: "Явная service/module declaration.", example: "settings.gradle явно включает модуль orders-service." },
  HIGH: { label: "Подтверждено", meaning: "Есть прямой синтаксический факт или подтверждённое GigaCode-сопоставление.", source: "Аннотация, literal contract, парные evidence либо GigaCode verification.", example: "@PostMapping(\"/orders\") напрямую подтверждает HTTP-вход." },
  MEDIUM: { label: "Вероятно", meaning: "Связь выведена статически, но часть семантики восстановлена эвристикой.", source: "Call tracing, contract-only match, неявное имя поля/колонки.", example: "createOrder() вызывает поле orderService, тип которого восстановлен по коду." },
  LOW: { label: "Сомнительно", meaning: "Исходящий target hint найден, но не сопоставлен с известным сервисом.", source: "Неоднозначная или внешняя цель без подтверждённого входного контракта.", example: "paymentClient найден, но payment-service среди подключённых репозиториев не обнаружен." },
  UNRESOLVED: { label: "Не определено", meaning: "Цель отсутствует, содержит placeholder или допускает несколько сервисов.", source: "Неразрешённая конфигурация либо неоднозначный match.", example: "URL ${PAYMENT_URL} не разрешился из конфигурации." },
};

export const EDGE_ORIGIN_LABELS: Record<string, string> = {
  declared: "явная декларация",
  static: "статический анализ",
  gigacode: "найдено GigaCode",
  "static+gigacode": "статика + проверка GigaCode",
};

export function nodeColor(type: string): string {
  return ({ Service: "#b6f36b", ExternalSystem: "#ffb45d", BusinessOperation: "#78a7ff", BusinessRule: "#df83ff", EntryPoint: "#6ee7d8", ExitPoint: "#ff7e67", Event: "#ff7690", Table: "#f4d269", DomainEntity: "#a78bfa", Repository: "#8795aa" } as Record<string, string>)[type] || "#728096";
}

export function confidenceColor(confidence: string): string {
  return ({ DECLARED: "#55e89a", HIGH: "#74a7ff", MEDIUM: "#f3c76b", LOW: "#ff9b55", UNRESOLVED: "#ff5f7d" } as Record<string, string>)[confidence] || "#8795aa";
}

export type EdgeServiceScope = "internal" | "cross" | "unknown";

export const EDGE_SCOPE_INFO: Record<EdgeServiceScope, { label: string; meaning: string; color: string }> = {
  internal: { label: "Внутри сервиса", meaning: "Оба конца имеют одинаковый service_id.", color: "#78a7ff" },
  cross: { label: "Между сервисами", meaning: "У концов разные service_id.", color: "#ff9b55" },
  unknown: { label: "Граница неизвестна", meaning: "Хотя бы у одного конца нет service_id.", color: "#8795aa" },
};

export function graphNodeServiceId(node: { id: string; type: string; service_id: string | null; metadata: Record<string, unknown> }): string | null {
  if (node.service_id) return node.service_id;
  if (node.type === "Service") return node.id;
  const metadataService = node.metadata?.service_id;
  return typeof metadataService === "string" && metadataService ? metadataService : null;
}

export function serviceColor(serviceId: string): string {
  let hash = 2166136261;
  for (let index = 0; index < serviceId.length; index += 1) {
    hash ^= serviceId.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  const hue = Math.abs(hash) % 360;
  const saturation = 62 + (Math.abs(hash >>> 8) % 18);
  const lightness = 56 + (Math.abs(hash >>> 16) % 12);
  return `hsl(${hue}, ${saturation}%, ${lightness}%)`;
}

export function edgeServiceScope(
  edge: { source: string | { id: string }; target: string | { id: string } },
  nodesById: Map<string, { id: string; type: string; service_id: string | null; metadata: Record<string, unknown> }>,
): EdgeServiceScope {
  const sourceId = typeof edge.source === "object" ? edge.source.id : edge.source;
  const targetId = typeof edge.target === "object" ? edge.target.id : edge.target;
  const source = nodesById.get(sourceId);
  const target = nodesById.get(targetId);
  if (!source || !target) return "unknown";
  const sourceService = graphNodeServiceId(source);
  const targetService = graphNodeServiceId(target);
  if (!sourceService || !targetService) return "unknown";
  return sourceService === targetService ? "internal" : "cross";
}

export function nodeTypeTooltip(type: string, visible: number, total: number): string {
  const info = NODE_TYPE_INFO[type];
  if (!info) return `${type}. Нажатие показывает или скрывает этот тип узлов. Сейчас ${visible} из ${total}.`;
  return `${info.label} (${type}). ${info.meaning} Пример: ${info.example} Источник: ${info.source} Нажатие показывает или скрывает тип. Сейчас ${visible} из ${total}.`;
}

export function confidenceTooltip(confidence: string): string {
  const info = CONFIDENCE_INFO[confidence];
  if (!info) return `${confidence}. Нажатие показывает или скрывает связи этой уверенности.`;
  return `${info.label} (${confidence}). ${info.meaning} Пример: ${info.example} Источник: ${info.source} Нажатие показывает или скрывает такие связи.`;
}
