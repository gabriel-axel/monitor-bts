# Radar ARIRANG 💜

Monitor de ingressos do **BTS WORLD TOUR 'ARIRANG'** no Estádio MorumBIS (28, 30 e 31/10/2026), feito por fã pra fã.

Painel: **https://gabriel-axel.github.io/monitor-bts/**

A cada ~5 min (GitHub Actions) o radar checa:

- **Ticketmaster**: se alguma data sai de "Esgotado" ou aparece página nova.
- **BuyTicket**: menor preço por data, setor e categoria.
- **Bluesky e Reddit**: anúncios de fãs, com detector de golpe (texto copiado entre contas e frases de roteiro).
- **Google News**: notícias sobre ingressos, transferência e Quentro.

Alertas vão por [ntfy](https://ntfy.sh). O tópico fica no segredo `NTFY_TOPICO` do repositório.

Não é site oficial. Transferência de ingresso só pelo app Quentro, e cada ingresso transfere uma única vez.
