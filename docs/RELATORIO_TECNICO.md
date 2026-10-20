# Relatório técnico — viabilidade da interoperabilidade universal (OmniLink v0.1)

*Data: 18/10/2026 · Ambiente de validação: Linux (Ubuntu 24), Python 3.12.3, Flask 3.1.3, Werkzeug 3.1.7, Pillow 12.1.1,
requests 2.33.1, SQLite 3.45.1, Chromium 141 (Playwright 1.56). Sem acesso à internet durante a construção, exceto consulta à
documentação do Render.*

Este relatório cobre a **Etapa A (pesquisa)** e, ao final, os resultados das etapas C–D. A arquitetura (Etapa B) está em
[ARQUITETURA_E_PROTOCOLO.md](ARQUITETURA_E_PROTOCOLO.md), a hospedagem (Etapa E) em [DEPLOY_RENDER.md](DEPLOY_RENDER.md) e a
expansão (Etapa F) na seção 11.

## 1. Resumo e veredito

A hipótese — *dispositivos heterogêneos podem compartilhar capacidades que permitam comunicação com o mínimo de
configuração manual* — **é verdadeira sob uma condição**: cada ponta precisa oferecer **ao menos um mecanismo comum já
existente e aceitar participar**. Sob essa condição foi demonstrado, com código e testes, descobrir, anunciar
capacidades, negociar versão/transporte/codec/formato, adaptar o conteúdo, autorizar, entregar e confirmar. Fora dessa
condição, a interoperabilidade não é "difícil": é **impossível sem violar proteções**, o que está explicitamente fora do escopo.

| Categoria | Conclusão | Evidência |
|---|---|---|
| **Possível e demonstrado** | Entre dispositivos que executam um agente OmniLink (cliente Python) ou um navegador moderno com HTTPS: descoberta via coordenador, negociação automática, entrega de texto/imagem/HTML com confirmação, adaptação ao receptor, recuperação após quedas | 263 testes automatizados, demonstração local e testes em Chromium real (seção 8) |
| **Exige suporte prévio** | TVs, impressoras, lâmpadas, alto-falantes e qualquer aparelho sem agente: dependem de **adaptadores** para o protocolo que *eles* já falam (Cast, AirPlay, DLNA/UPnP, Matter…), cada um com pareamento, autenticação e, às vezes, programa de certificação do fabricante | `discovery/catalog.py` classifica o que cada tipo exigiria; nenhum adaptador foi implementado |
| **Não pode ser garantido** | "Comunicação universal com qualquer dispositivo, sem dependência de fabricante e sem cooperação dele" | Argumento das seções 3 e 4; não existe técnica legítima que o torne verdadeiro |

O OmniLink é, portanto, melhor descrito como **uma camada de descoberta e negociação sobre padrões abertos onde eles
existem**, e não como um "idioma universal".

## 2. A hipótese, reformulada para ser testável

Critérios derivados do enunciado e o resultado de cada um:

| # | Critério | Resultado | Observação |
|---|---|---|---|
| 1 | Descobrir dispositivos e serviços acessíveis | ◐ parcial | Registro no coordenador ✔; LAN por UDP próprio, só entre peers OmniLink, testado em loopback; mDNS/SSDP **não** implementados |
| 2 | Identificar capacidades de comunicação | ✔ | Documento de capacidades versionado, com validação estrita |
| 3 | Comparar protocolos e formatos | ✔ | Interseção de versões, transportes, codecs, tipos e limites |
| 4 | Encontrar combinação compatível automaticamente | ✔ | Motor puro com pontuação explicável (2 transportes × 2 codecs × 3 tipos de conteúdo) |
| 5 | Selecionar canal adequado | ✔ | Considera tamanho do conteúdo, sessão ativa e política TLS |
| 6 | Adaptar mensagens ao receptor | ✔ | HTML→texto; imagem→reduzir/recodificar; nada é descartado em silêncio |
| 7 | Transmitir texto, imagens e HTML | ✔ | Incluindo HTML hostil neutralizado (servidor, receptor e navegador) |
| 8 | Reduzir configuração manual | ◐ | Uma chave de registro por dispositivo; a **autorização explícita é intencional** e permanece |
| 9 | Incorporar novos protocolos sem reescrever | ✔ | Testado: transporte e codec plugados em um registro; "novo" = escrito por um desenvolvedor, não aprendido |
| 10 | Ampla interoperabilidade sem depender de fabricante | ✗ | Não demonstrável; só existe onde há cooperação (seções 3–4) |

## 3. Por que uma representação binária comum não garante comunicação

Todo dado digital é uma sequência de bits — mas bits **não carregam significado**. Para que uma máquina "entenda" outra,
várias camadas independentes precisam coincidir, e o bit é apenas a mais baixa delas:

| Camada | Pergunta que precisa ter resposta | Se falhar |
|---|---|---|
| Física/enlace | Os dois estão na mesma rede (Wi-Fi, Bluetooth, cabo)? | Nem o sinal chega |
| Rede | Há rota? NAT, CGNAT (RFC 6598), firewall e isolamento de clientes bloqueiam entradas | O pacote é descartado |
| Transporte | Alguém **está escutando** nessa porta, com TCP/UDP/QUIC? | Conexão recusada / sem resposta |
| Segurança | Quem pode falar? Como provar identidade? Há TLS? | Conexão aberta, mas ação negada |
| Aplicação — *sintaxe* | Como delimitar mensagens, que codificação (UTF-8, Base64, binário)? | Lixo ou erro de análise |
| Aplicação — *semântica* | O que significa "exibir isto"? Que tipos de mídia são entendidos? | Dados recebidos e ignorados |
| Aplicação — *pragmática* | Em que ordem, com que confirmação, com que permissão? | Estado inconsistente |

Analogia: duas pessoas que usam o mesmo alfabeto latino não se entendem se uma escreve em português e a outra em
japonês romanizado. **Compartilhar o alfabeto (bits) é necessário, não suficiente.** Na prática, para receber e interpretar
informações, um dispositivo precisa **(a)** estar acessível, **(b)** ter um processo escutando, **(c)** falar um protocolo de aplicação
conhecido, **(d)** ter um renderizador para o tipo de conteúdo e **(e)** aceitar o remetente. Cada item é uma capacidade que o
dispositivo *tem ou não tem* — e é exatamente o que o documento de capacidades anuncia.

Exemplo concreto: enviar os bytes de um PNG, por TCP, a uma TV em porta arbitrária. Sem ouvinte, a conexão é recusada. Com
ouvinte de outro protocolo (por exemplo, Cast), os bytes são lidos como mensagem malformada. Mesmo com o protocolo certo,
a TV exige pareamento/aplicativo registrado. Só uma TV que **oferece** um receptor compatível (um navegador acessando
`/receiver`, ou um app OmniLink) recebe e exibe. Por isso o OmniLink não "empurra" bytes: ele **negocia e depende de um receptor cooperante**.

## 4. Detectar, conectar e agir são degraus distintos

| Degrau | Exemplo no mundo real | No OmniLink | Falha diagnosticada como |
|---|---|---|---|
| **Detectar** | mDNS lista `_googlecast._tcp` de uma TV | `GET /v1/devices`, descoberta UDP | `RECEIVER_UNKNOWN` |
| **Conectar** | Abrir TCP/TLS na porta 8009 da TV é possível | Sessão SSE/long-poll com token válido; negociação de versão/transporte/codec | `NO_COMMON_TRANSPORT`, `NO_COMMON_CODEC`, `PROTOCOL_VERSION_MISMATCH`, `TLS_REQUIRED` |
| **Agir** | Exibir HTML arbitrário na TV exige protocolo Cast + app receptor registrado + sessão | Autorização por tipo (`grant`) + conteúdo aceito pelo receptor | `NOT_AUTHORIZED`, `CONTENT_UNSUPPORTED`, `SIZE_LIMIT_EXCEEDED` |

Nenhum degrau implica o seguinte. O sistema nunca tenta "forçar" o degrau seguinte: se o dispositivo não oferece um
mecanismo, o resultado é um diagnóstico com alternativas — não uma tentativa de contornar proteções.

## 5. Análise técnica dos dez tópicos

1. **Representação binária e codificação.** UTF-8 para texto; Base64 (+33 % de bytes) para binário em JSON; *frame* binário
   próprio (16 bytes de cabeçalho fixo + JSON + payload) que economizou **25 %** dos bytes em imagens (seção 8.3);
   SHA-256 do conteúdo em toda mensagem para detectar corrupção. CBOR/MessagePack/Protobuf seriam mais compactos para
   metadados, mas perdem legibilidade e suporte nativo no navegador; ficam como *plugins* de codec.
2. **Protocolos de rede e transporte.** IP/TCP/UDP como base; HTTP(S) como denominador comum (todo navegador e toda linguagem);
   TLS 1.3 (RFC 8446) para confidencialidade, terminado pelo Render. Para "servidor→receptor em tempo real" comparei
   WebSocket (RFC 6455), SSE e *long polling* (RFC 6202). **WebSocket não é necessário** para os fluxos atuais (eventos raros
   servidor→receptor; o restante é requisição/resposta) e exigiria dependência indisponível aqui; SSE + long-poll entregam em
   ≈ 3 ms (loopback) e atravessam proxies sem configuração.
3. **Descoberta de dispositivos e serviços.** O padrão é mDNS (RFC 6762) + DNS-SD (RFC 6763), multicast em 224.0.0.251:5353;
   SSDP/UPnP (239.255.255.250:1900) para TVs/DLNA. Limites: **não atravessam roteadores/VLANs**, são bloqueados por *client
   isolation* em redes de convidados e não existem na nuvem. Implementei um protocolo UDP mínimo (só entre peers OmniLink; com
   proteção contra amplificação) e um **catálogo** que explica o que cada tipo mDNS/SSDP exigiria. mDNS real fica para a etapa F.
4. **Negociação automática de capacidades.** Inspirada em negociação de conteúdo HTTP (`Accept`), ALPN do TLS e oferta/resposta
   SDP do WebRTC: ambos anunciam, o motor intersecta e pontua, e **explica** descartes (`considered`).
5. **Interoperabilidade entre protocolos.** Com *n* protocolos, tradução par-a-par custa *n²* adaptadores; um **protocolo pivô**
   (o do OmniLink) com adaptadores nas bordas custa *n*. Os adaptadores seguem o padrão de plugin já testado, mas dependem de
   cada ecossistema (seção 6).
6. **Transmissão de imagens, texto e HTML.** Conteúdo estático não precisa de *streaming* de mídia (HLS, RFC 8216; RTSP;
   WebRTC). O risco real é **conteúdo ativo**: o HTML passa por (1) sanitização por lista de permissão no servidor, (2)
   re-sanitização no receptor Python e (3) `iframe sandbox` + CSP no navegador; imagens são decodificadas por completo, com
   limite de pixels e verificação de dados anexados.
7. **Navegadores e receptores compatíveis.** O navegador é o receptor mais universal (quase todo aparelho com tela tem um).
   Navegadores antigos sem `ReadableStream` anunciam só `http-longpoll` e continuam funcionando — a negociação *acontece
   dentro do navegador* (testado). Smart TVs com navegador podem abrir `/receiver`; TVs sem navegador, não.
8. **Comunicação local × internet.** Um servidor na nuvem **não alcança** a LAN do usuário (NAT/CGNAT/firewall). A solução
   adotada: todos os dispositivos abrem conexões de **saída** autenticadas até o coordenador (que também roda localmente no
   notebook). Custo: o relé enxerga o conteúdo (sem criptografia ponta a ponta nesta versão).
9. **Autenticação, autorização e proteção.** Chave de registro → token por dispositivo (256 bits, só o hash SHA-256 é
   guardado) → autorização **por tipo de conteúdo**, concedida pelo receptor → limites de tamanho/taxa → revogação imediata
   → auditoria sem segredos. Detalhes e ameaças residuais em [SEGURANCA.md](SEGURANCA.md).
10. **Limitações de hardware, firmware, SO e fabricantes.** Firmwares de TV fechados; Cast/AirPlay exigem receptor
    licenciado/registrado; iOS/Android restringem processos em segundo plano (um app receptor precisaria de serviço em
    primeiro plano ou notificações *push*); DRM/HDCP limitam espelhamento; redes corporativas bloqueiam multicast e portas.
    Nada disso é contornável de forma legítima por software de terceiros.

## 6. Tecnologias avaliadas

| Tecnologia | Adequação ao projeto | Decisão na v0.1 |
|---|---|---|
| HTTP/1.1 (RFC 9112), HTTP/2 | Universal; semântica comum (RFC 9110) | **Usado** (REST) |
| TLS 1.3 / HTTPS | Obrigatório para confidencialidade na internet | **Exigido em produção**; terminado pelo Render |
| WebSocket (RFC 6455) | Bidirecional; exige biblioteca/servidor ASGI; cai em redeploys/spin-down | **Não usado** (desnecessário agora); plugin previsto |
| Server-Sent Events | Push unidirecional simples, sobre HTTP | **Usado** (`sse`) |
| Long polling (RFC 6202) | Funciona em qualquer cliente HTTP | **Usado** (`http-longpoll`) |
| WebRTC (RFC 8825, ICE RFC 8445) | P2P e grandes volumes; precisa de sinalização, STUN/TURN | Não usado; o coordenador poderia ser o servidor de sinalização |
| mDNS (RFC 6762) / DNS-SD (RFC 6763) | Padrão para descoberta na LAN; amplo (Cast, AirPlay, impressoras) | Não implementado (sem biblioteca aqui); **catálogo** + UDP próprio |
| SSDP / UPnP / DLNA | Descoberta e controle de renderizadores de mídia | Apenas catalogado |
| Google Cast | Proprietário; receptor registrado; mDNS `_googlecast._tcp` | Apenas catalogado; fora do escopo sem adaptador oficial |
| AirPlay | Proprietário (Apple); pareamento; mDNS `_airplay._tcp` | Apenas catalogado |
| Miracast / Wi-Fi Direct | Espelhamento ponto a ponto no nível do SO | Fora do escopo (não é rede de dados genérica) |
| DIAL | Lança apps registrados em TVs; não exibe conteúdo arbitrário | Apenas catalogado |
| Matter | Casa inteligente; comissionamento em *fabric* | Fora do escopo |
| Bluetooth LE | Curto alcance; pareamento; MTU pequeno | Fora do escopo |
| MQTT | Pub/sub com *broker*; ótimo para IoT | Candidato a transporte futuro |
| HLS (RFC 8216) / RTSP / DASH | Mídia contínua | Desnecessário para texto/imagem/HTML |
| JSON / CBOR / MessagePack / Protobuf | JSON legível e nativo; os demais mais compactos | JSON + *frame* binário próprio; demais como plugin |

## 7. Decisões de arquitetura e justificativas

| Decisão | Justificativa | Custo/risco |
|---|---|---|
| **Flask** (e não FastAPI) | Disponível e testável no ambiente sem rede (FastAPI/uvicorn/pydantic ausentes); suficiente para requisição/resposta + SSE/long-poll; implantação simples com gunicorn. A lógica está em `app/coordinator`, **independente de framework** — trocar por ASGI é local | Sem `async`/WebSocket nativos; 1 thread por fluxo aberto |
| **SSE + long-poll** em vez de WebSocket | Ver tópico 2; evita dependência e é mais simples de operar no plano gratuito | Sem canal bidirecional único (acks vão por `POST`) |
| **Python** como linguagem principal; **Java não usado** | Protótipo rápido, testável, uma só linguagem. Java só teria vantagem concreta num **receptor Android/Android TV nativo** — registrado no roadmap, não no servidor | — |
| `Store` abstrato; **SQLite/PostgreSQL** | Sem banco, qualquer *spin-down* do Render apaga dispositivos e tokens. PostgreSQL é *necessário* para continuidade; SQLite serve para testes/desenvolvimento | PostgreSQL não validado aqui; esquema "chave-valor" simples (não otimizado) |
| Sanitizador de HTML na biblioteca padrão | Sem dependências; reconstrói a saída a partir de tokens (nada copiado) | Código próprio; avaliar `nh3` se os requisitos crescerem |
| Descoberta UDP própria | Testável aqui; interface trocável por mDNS | Só entre peers OmniLink; sem autenticação |
| Testes com `unittest` | pytest ausente no ambiente; a suíte também roda com pytest | — |
| Instância única | Sinais de despertar e "online" ficam em memória do processo | Não escala horizontalmente sem Redis/NOTIFY |

## 8. Validação experimental

### 8.1 Testes automatizados — 263 testes, todos aprovados (execução final, ≈ 73 s)

Comando: `python -m unittest discover -s tests -t . -v`.

| Arquivo | Testes | Cobre (requisito da seção 11 do enunciado) |
|---|---|---|
| `test_codec` | 22 | Serialização/desserialização (JSON e binário), quadros malformados, integridade, plugin de codec |
| `test_protocol_engine` | 22 | Negociação de capacidades, seleção de protocolos, incompatibilidades, política TLS, extensibilidade |
| `test_content` | 23 | Validação de arquivos e entradas (HTML hostil, imagens corrompidas/poliglotas/bombas), adaptação |
| `test_security` | 19 | Tokens, limitador de taxa, validação, redação de logs, configuração, regressão de regex |
| `test_store` | 14 | Armazenamento (memória e SQLite com o mesmo contrato), expiração, injeção SQL, persistência |
| `test_coordinator` | 36 | Autorização, entrega, reentrega, expiração, concorrência, **recuperação após reinício**, revogação |
| `test_api` | 44 | Endpoints, autenticação/autorização, limites (413/429), TLS (426), SSE, auditoria sem segredos |
| `test_client_resilience` | 26 | Falhas de conexão, **reconexão com tentativas limitadas**, deduplicação, receptor que não confia no relé |
| `test_e2e_real_sockets` | 12 | Servidor e clientes reais: SSE, long-poll binário, adaptação, fila offline, ack perdido, queda e retorno do servidor |
| `test_browser_receiver` | 11 | **Chromium real**: registro, consentimento, texto/imagem/HTML, reconexão, *fallback* long-poll, defesa em profundidade |
| `test_cli` | 11 | CLI (credenciais 0600, token nunca impresso, códigos de saída) |
| `test_discovery` | 12 | UDP (anti-amplificação, limite por origem, validação de respostas), catálogo |
| `test_production_profile` | 6 | Perfil de produção (HTTPS atrás do proxy, logs JSON sem segredos, falha rápida, `python -m app.api`) |
| `test_examples` | 4 | Demonstração e verificação pós-implantação funcionam |
| `test_readme_flow` | 1 | Os comandos documentados no README, executados como processos reais (servidor, registro, `receive`, `send`) |

Ainda **não** testado: ver seção 9.

### 8.2 Defeitos encontrados *pelos testes* e corrigidos

* `$` em expressões regulares aceitava um `\n` final em IDs/tokens (`"dev_…\n"`) → trocado por `\Z` em todo o projeto, com teste de regressão.
* Imagens com dados **anexados** após o fim (técnica de arquivos poliglotas) eram aceitas → verificação de término para PNG/GIF/WebP (JPEG isento: câmeras gravam dados extras legítimos).
* Receptores *long-poll* só ficavam "conectados" após a primeira espera (até 20 s) → a primeira consulta passou a ser imediata.
* Os testes de queda do servidor enganavam-se com conexões *keep-alive* "zumbis" do servidor de desenvolvimento → passaram a simular a queda de fato.

### 8.3 Medições (loopback; `examples/benchmark_local.py`)

```
Latência de entrega (POST /v1/messages → apresentação no receptor), texto curto, 40 envios, loopback
transporte        mediana      p95      máx
sse/json            3.1ms    4.8ms    6.3ms
http-longpoll/json  3.0ms    4.6ms    5.1ms
http-longpoll/binary 5.4ms   6.9ms    7.6ms

Custo em bytes no fio por codec (imagem PNG incompressível)
imagem          conteúdo    json-b64  binary-frame  economia
200x150           90.275     120.688        90.601     24.9%
640x480          923.409   1.231.533       923.736     25.0%
900x700        1.893.368   2.524.814     1.893.696     25.0%
```

Interpretação: o custo do próprio software é de poucos milissegundos; **não inclui** rede, TLS nem o proxy do Render. O
codec binário poupa 25 % dos bytes, mas custa uma requisição a mais (≈ 2 ms) — por isso o motor prefere SSE+JSON para
conteúdos pequenos e long-poll+binário para os grandes.

### 8.4 Demonstração (`examples/demo_local.py`; saída completa em `docs/demo_output.txt`)

Mostra, em sequência: registro com capacidades diferentes; a **mesma imagem** negociada com três planos distintos
(SSE+JSON; long-poll+binário; long-poll+JSON com redução) e um dispositivo incompatível explicado (5 diagnósticos, 4 alternativas); recusa sem autorização e entrega após aprovação;
HTML hostil entregue já sanitizado; imagem de ~920 KB reduzida a 7,6 KB (JPEG 160×120) para o painel limitado; fila para
receptor offline; ack perdido com reentrega deduplicada; revogação imediata.

### 8.5 Defesa em profundidade no navegador (teste em Chromium real)

Com a sanitização do servidor **desligada de propósito** e um HTML com `<script>`, `<img onerror>`, `<meta refresh>`,
`<form>`, `<a target=_blank>` e `<iframe>` externo, o navegador: não executou o script; recusou o redirecionamento
("The document is sandboxed"); bloqueou a imagem e o `iframe` externos pela CSP (nenhuma resposta de rede recebida); e
impediu o envio do formulário e a abertura do link. A própria CSP da página também bloqueou o `eval` que a ferramenta de
teste tentou usar — evidência de que a política está ativa.

## 9. Demonstrado × implementado sem validação × não implementado

| Item | Situação |
|---|---|
| API REST, SSE, long-poll, codecs JSON/binário, negociação, adaptação, autorização, revogação, fila, acks, reentrega | **Demonstrado** (testes) |
| Receptor web em Chromium 141 | **Demonstrado**; outros navegadores/versões **não testados** |
| Persistência SQLite e recuperação após reinício | **Demonstrado** |
| Descoberta UDP | **Demonstrado em loopback (unicast)**; broadcast/multicast em LAN real **não testados** |
| Armazenamento **PostgreSQL** | Implementado; **não validado** (sem PostgreSQL/driver aqui) |
| `gunicorn` e `render.yaml` | Escritos conforme a documentação; **não executados** (sem gunicorn/rede aqui); `render.yaml` validado apenas como YAML |
| Implantação real no Render, TLS e proxy reais | **Não realizados**; `examples/smoke_remote.py` verifica em minutos após o *deploy* |
| Windows e macOS | **Não testados** (apenas Linux) |
| WebSocket, mDNS/DNS-SD, SSDP, `lan-direct`, adaptadores Cast/AirPlay/DLNA, receptor Android/TV, criptografia ponta a ponta, contas e multi-inquilino, várias instâncias | **Não implementados** (seção 11) |

## 10. Riscos e limitações conhecidas

* **O coordenador lê o conteúdo** (relé sem criptografia ponta a ponta). Confie apenas em um coordenador que você controla.
* **Instância única.** Limitadores de taxa, "online" e sinais de despertar vivem em memória do processo; não use vários *workers*.
* **1 thread por fluxo aberto.** Com `--threads 16`, cerca de 16 receptores simultâneos; ajuste conforme o plano.
* **Plano gratuito do Render:** o serviço dorme após 15 min sem tráfego (≈ 1 min para acordar); o PostgreSQL gratuito **expira em 30 dias**.
* **IP do cliente atrás do proxy:** o limite por IP assume 1 salto de proxy (`X-Forwarded-For`); o número exato de saltos do Render não foi confirmado. O limite por dispositivo é o controle principal.
* **Um único domínio de confiança:** quem tem a chave de registro registra dispositivos e os revoga; não há contas.
* **Descoberta UDP sem autenticação:** respostas são validadas, mas qualquer host da LAN pode anunciar-se; nunca envie credenciais a um endereço apenas "descoberto".
* **Sanitizador de HTML próprio** (biblioteca padrão) + sandbox; revisar com `nh3` se o conjunto de tags crescer.
* **Metadados de imagem (EXIF) não são removidos** quando não há adaptação.
* Processamento de imagem consome CPU/memória: limites padrão de 2 MB e 8 megapixels; o plano gratuito tem pouca CPU.

## 11. Próximos passos — Etapa F (expansão)

Ordem sugerida (valor ÷ risco):

1. **Validar PostgreSQL e o *deploy* real** (CI com serviço PostgreSQL; executar `smoke_remote.py` no Render); trocar o esquema chave-valor por tabelas e índices.
2. **Receptor Android/Android TV** (Java ou Kotlin): um app que reutiliza o protocolo (SSE/long-poll + `POST` de ack) — aqui Java *tem* vantagem concreta, pois dá acesso a serviço em primeiro plano, notificações e à tela. Alternativa de custo zero: abrir `/receiver` no navegador do aparelho.
3. **mDNS/DNS-SD** com `zeroconf` (anúncio do coordenador/receptores e navegação), mantendo o catálogo para classificar o que for encontrado.
4. **Transporte `ws`** (plugin) para tráfego bidirecional de baixa latência, mantendo SSE/long-poll como *fallback*.
5. **Transporte `lan-direct`** com pareamento (PIN/QR → chave mútua) para operar sem nuvem.
6. **Criptografia ponta a ponta** (chaves por dispositivo trocadas na autorização) para que o relé não leia o conteúdo.
7. **Adaptadores** (um por ecossistema, só para dispositivos que os aceitam legitimamente): DLNA/UPnP (enviar URL de mídia), Cast (receptor padrão de mídia) e AirPlay — **sem** HTML arbitrário e respeitando pareamento/licenças.
8. **WebRTC** (canal de dados) para arquivos grandes, com o coordenador como sinalização.
9. **Contas e escopos**, múltiplas instâncias (Redis ou PostgreSQL `LISTEN/NOTIFY`), métricas e rastreamento.
