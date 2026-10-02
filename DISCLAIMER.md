# Disclaimer / Haftungsausschluss

[English](#english) | [Deutsch](#deutsch)

## English

**Not financial or investment advice.** glass-quant is open-source software for education and research. Nothing in this repository, its documentation, its code comments, its example outputs or its issue and pull request discussions is financial advice, investment advice, tax advice or legal advice. Nothing in it is a recommendation or an offer to buy, sell or hold any security, fund, cryptocurrency or other financial instrument. Scan results, scores, candidate lists, regime labels and order plans are outputs of a research program. They do not take your personal situation, goals or risk tolerance into account.

**Education and research only.** The project exists to study systematic trading ideas and to test them honestly. It has not been shown to make money, and it has not made anyone rich. Use it to learn, to experiment and to improve the code.

**Risk of total loss.** Trading and investing involve substantial risk. You can lose part or all of the money you invest. With margin, short selling or leveraged products, losses can be larger than your deposit. Software bugs, wrong or late data, outages of data providers or brokers, network problems and configuration mistakes can all cause losses.

**Past and backtest performance is not indicative of future results.** Backtests and simulations in this repository are hypothetical. They were run on historical data with simplifying assumptions, for example about costs, slippage, fills and survivorship, and the data used for them is not included. Hypothetical results have well-known limits: they are produced with hindsight and do not involve real money or real execution.

**Paper trading by default. Live trading at your own risk.** The execution bot runs against a paper (simulated) account by default, and live trading stays locked until you unlock it on purpose. If you trade live, you do so entirely at your own risk and responsibility. You decide what is traded, and you are responsible for every order your setup sends, for your broker account, for your API keys and for complying with the laws, tax rules and broker terms that apply to you.

**No warranty, no liability.** The software is provided "as is", without warranty of any kind, to the extent permitted by applicable law. The authors and contributors are not liable for any damages or losses that result from using it, to the extent permitted by applicable law. See section 15 (Disclaimer of Warranty) and section 16 (Limitation of Liability) of the GNU Affero General Public License v3.0 in [LICENSE](LICENSE).

**Third-party data and services.** The project uses free third-party data sources and APIs, for example Yahoo Finance through yfinance, Stooq, FRED, SEC EDGAR, CBOE, GDELT, Google News RSS (local dashboard), Reddit and Alpaca, plus the opt-in website scrapers for openinsider and capitoltrades. Their data can be wrong, late or incomplete. You are responsible for following their terms of use. This project is not affiliated with, endorsed by or sponsored by any of them.

**Tax tools are not tax advice.** `tools/tax_loss_harvester.py` is a calculator for research purposes. Its rules, for example the loss offset limits for German tax pots, may be outdated or wrong. Ask a qualified tax advisor before you act on anything tax related.

**Sharing outputs.** If you publish signals, candidate lists or other outputs that you generate with this software, you are responsible for complying with the rules that apply to you, for example the rules on investment recommendations in the EU Market Abuse Regulation (MAR).

## Deutsch

**Keine Anlageberatung, keine Anlageempfehlung.** glass-quant ist Open-Source-Software für Bildungs- und Forschungszwecke. Nichts in diesem Repository, in der Dokumentation, in Code-Kommentaren, in Beispielausgaben oder in Diskussionen zu Issues und Pull Requests ist eine Anlageberatung, eine Anlageempfehlung, eine Finanz-, Steuer- oder Rechtsberatung. Nichts davon ist eine Empfehlung oder ein Angebot, Wertpapiere, Fonds, Kryptowerte oder andere Finanzinstrumente zu kaufen, zu verkaufen oder zu halten. Scan-Ergebnisse, Scores, Kandidatenlisten, Regime-Einstufungen und Orderpläne sind Ausgaben eines Forschungsprogramms. Sie berücksichtigen weder Ihre persönliche Situation noch Ihre Ziele oder Ihre Risikobereitschaft.

**Nur für Bildung und Forschung.** Das Projekt dient dazu, systematische Handelsideen zu untersuchen und ehrlich zu testen. Es ist nicht belegt, dass es Geld verdient, und es hat niemanden reich gemacht. Nutzen Sie es zum Lernen, zum Experimentieren und um den Code zu verbessern.

**Risiko des Totalverlusts.** Handel und Geldanlage sind mit erheblichen Risiken verbunden. Sie können einen Teil oder Ihr gesamtes eingesetztes Kapital verlieren. Mit Margin, Leerverkäufen oder gehebelten Produkten können die Verluste sogar höher sein als Ihre Einlage. Softwarefehler, falsche oder verspätete Daten, Ausfälle von Datenanbietern oder Brokern, Netzwerkprobleme und Konfigurationsfehler können ebenfalls zu Verlusten führen.

**Vergangene Wertentwicklungen und Backtests sind kein verlässlicher Indikator für zukünftige Ergebnisse.** Backtests und Simulationen in diesem Repository sind hypothetisch. Sie beruhen auf historischen Daten und vereinfachenden Annahmen, zum Beispiel zu Kosten, Slippage, Ausführung und Survivorship-Bias, und die verwendeten Daten sind nicht enthalten. Hypothetische Ergebnisse haben bekannte Grenzen: Sie entstehen im Rückblick und ohne echtes Geld und echte Orderausführung.

**Standardmäßig Papierhandel. Live-Handel auf eigenes Risiko.** Der Ausführungs-Bot arbeitet standardmäßig mit einem Papierkonto (Simulation), und der Live-Handel bleibt gesperrt, bis Sie ihn bewusst freischalten. Wenn Sie live handeln, geschieht das ausschließlich auf Ihr eigenes Risiko und in Ihrer eigenen Verantwortung. Sie entscheiden, was gehandelt wird, und Sie sind verantwortlich für jede Order, die Ihr Setup sendet, für Ihr Brokerkonto, für Ihre API-Schlüssel und für die Einhaltung der Gesetze, Steuerregeln und Broker-Bedingungen, die für Sie gelten.

**Keine Gewährleistung, keine Haftung.** Die Software wird ohne jede Gewährleistung bereitgestellt ("as is"), soweit gesetzlich zulässig. Die Autorinnen, Autoren und Mitwirkenden haften nicht für Schäden oder Verluste, die aus der Nutzung entstehen, soweit gesetzlich zulässig. Siehe Abschnitt 15 (Disclaimer of Warranty) und Abschnitt 16 (Limitation of Liability) der GNU Affero General Public License v3.0 in der Datei [LICENSE](LICENSE).

**Daten und Dienste Dritter.** Das Projekt nutzt kostenlose Datenquellen und APIs Dritter, zum Beispiel Yahoo Finance über yfinance, Stooq, FRED, SEC EDGAR, CBOE, GDELT, Google News RSS (lokales Dashboard), Reddit und Alpaca, dazu die optionalen Website-Scraper für openinsider und capitoltrades. Deren Daten können falsch, verspätet oder unvollständig sein. Sie sind selbst dafür verantwortlich, deren Nutzungsbedingungen einzuhalten. Dieses Projekt steht mit keinem dieser Anbieter in Verbindung und wird von keinem unterstützt oder gesponsert.

**Steuer-Tools sind keine Steuerberatung.** `tools/tax_loss_harvester.py` ist ein Rechenwerkzeug für Forschungszwecke. Seine Regeln, zum Beispiel die Grenzen der Verlustverrechnung in deutschen Verlusttöpfen, können veraltet oder falsch sein. Fragen Sie eine Steuerberaterin oder einen Steuerberater, bevor Sie steuerlich handeln.

**Weitergabe von Ausgaben.** Wenn Sie Signale, Kandidatenlisten oder andere Ausgaben veröffentlichen, die Sie mit dieser Software erzeugen, sind Sie selbst dafür verantwortlich, die für Sie geltenden Regeln einzuhalten, zum Beispiel die Vorgaben der EU-Marktmissbrauchsverordnung (MAR) zu Anlageempfehlungen.
