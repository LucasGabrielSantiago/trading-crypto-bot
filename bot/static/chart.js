/* Gráfico de candles com indicadores, baseado na Lightweight Charts da TradingView. */
(() => {
  const LC = window.LightweightCharts;
  const GREEN = '#22C55E', RED = '#EF4444';
  const OVERLAYS = [
    { id: 'sma20', label: 'MM 20', keys: ['sma20'], color: '#7CC0FF', on: true },
    { id: 'sma50', label: 'MM 50', keys: ['sma50'], color: '#FFB454', on: true },
    { id: 'sma200', label: 'MM 200', keys: ['sma200'], color: '#E879F9', on: false },
    { id: 'ema9', label: 'MME 9', keys: ['ema9'], color: '#5EEAD4', on: false },
    { id: 'ema21', label: 'MME 21', keys: ['ema21'], color: '#FDE68A', on: false },
    { id: 'bollinger', label: 'Bollinger', keys: ['bbUpper', 'bbLower'], color: '#94A3B8', on: false },
  ];
  const PANELS = [
    { id: 'rsi', label: 'RSI 14', on: true },
    { id: 'macd', label: 'MACD', on: false },
  ];
  const STORAGE_KEY = 'bot-chart-indicators';
  const offset = new Date().getTimezoneOffset() * 60;
  const localTime = (time) => time - offset;
  const number = (value, digits) => Number(value).toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits });

  function savedChoices() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; } catch { return {}; }
  }
  function saveChoices(choices) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(choices)); } catch { /* opcional */ }
  }
  function precisionFor(price) {
    if (price >= 100) return 2;
    if (price >= 1) return 3;
    if (price >= 0.01) return 5;
    return 8;
  }
  function compact(value) {
    return Number(value).toLocaleString('pt-BR', { notation: 'compact', maximumFractionDigits: 2 });
  }

  class BotChart {
    constructor(host) {
      this.host = host;
      this.choices = savedChoices();
      [...OVERLAYS, ...PANELS].forEach((item) => {
        if (this.choices[item.id] === undefined) this.choices[item.id] = item.on;
      });
      this.data = null;
      this.levels = [];
      this.levelLines = [];
      this.fitted = false;
      this.buildShell();
      this.build();
    }

    buildShell() {
      this.host.replaceChildren();
      const tools = document.createElement('div');
      tools.className = 'chart-tools';
      tools.setAttribute('role', 'group');
      tools.setAttribute('aria-label', 'Indicadores do gráfico');
      [...OVERLAYS, ...PANELS].forEach((item) => {
        const button = document.createElement('button');
        button.className = 'chip chart-chip';
        button.textContent = item.label;
        button.setAttribute('aria-pressed', String(this.choices[item.id]));
        button.addEventListener('click', () => this.toggle(item.id, button));
        tools.appendChild(button);
      });
      const fit = document.createElement('button');
      fit.className = 'chip chart-chip';
      fit.textContent = 'Ajustar';
      fit.addEventListener('click', () => this.showRecent());
      tools.appendChild(fit);
      this.frame = document.createElement('div');
      this.frame.className = 'chart-frame';
      this.legend = document.createElement('div');
      this.legend.className = 'chart-legend';
      this.surface = document.createElement('div');
      this.surface.className = 'chart-surface';
      this.frame.append(this.legend, this.surface);
      this.host.append(tools, this.frame);
    }

    build() {
      const panes = PANELS.filter((item) => this.choices[item.id]).length;
      this.surface.style.height = `${450 + panes * 170}px`;
      this.chart = LC.createChart(this.surface, {
        autoSize: true,
        layout: { background: { type: 'solid', color: '#151C24' }, textColor: '#A9B6C3', fontFamily: 'inherit', panes: { separatorColor: '#3B4A5A' } },
        grid: { vertLines: { color: '#1F2933' }, horzLines: { color: '#1F2933' } },
        crosshair: { mode: LC.CrosshairMode.Normal },
        rightPriceScale: { borderColor: '#3B4A5A' },
        timeScale: { borderColor: '#3B4A5A', timeVisible: true, secondsVisible: false, rightOffset: 6 },
        localization: { locale: 'pt-BR' },
      });
      this.candle = this.chart.addSeries(LC.CandlestickSeries, {
        upColor: GREEN, downColor: RED, wickUpColor: GREEN, wickDownColor: RED, borderVisible: false,
      }, 0);
      this.volume = this.chart.addSeries(LC.HistogramSeries, {
        priceFormat: { type: 'volume' }, priceScaleId: 'volume', lastValueVisible: false, priceLineVisible: false,
      }, 0);
      this.volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      this.markers = LC.createSeriesMarkers(this.candle, []);
      this.lines = {};
      const line = (key, color, pane = 0, options = {}) => {
        this.lines[key] = this.chart.addSeries(LC.LineSeries, {
          color, lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, ...options,
        }, pane);
      };
      OVERLAYS.filter((item) => this.choices[item.id]).forEach((item) => item.keys.forEach((key) => line(key, item.color)));
      line('support', '#7CC0FF', 0, { lineStyle: LC.LineStyle.Dashed });
      line('resistance', '#E8EDF2', 0, { lineStyle: LC.LineStyle.Dashed });
      let pane = 1;
      if (this.choices.rsi) {
        line('rsi', '#C4B5FD', pane, { lastValueVisible: true });
        this.lines.rsi.createPriceLine({ price: 70, color: '#5B6B7C', lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: '' });
        this.lines.rsi.createPriceLine({ price: 30, color: '#5B6B7C', lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: '' });
        pane += 1;
      }
      if (this.choices.macd) {
        this.lines.macdHistogram = this.chart.addSeries(LC.HistogramSeries, { lastValueVisible: false, priceLineVisible: false }, pane);
        line('macd', '#7CC0FF', pane);
        line('macdSignal', '#FFB454', pane);
      }
      this.chart.panes().forEach((item, index) => { item.setStretchFactor(index ? 1 : 3); });
      this.chart.subscribeCrosshairMove((param) => this.showLegend(param.time));
    }

    toggle(id, button) {
      this.choices[id] = !this.choices[id];
      button.setAttribute('aria-pressed', String(this.choices[id]));
      saveChoices(this.choices);
      const range = this.chart.timeScale().getVisibleLogicalRange();
      this.chart.remove();
      this.build();
      if (this.data) this.fill(this.data);
      if (range) this.chart.timeScale().setVisibleLogicalRange(range);
      this.drawLevels();
    }

    series(key) {
      const values = this.data[key] || this.data.indicators[key] || [];
      const output = [];
      values.forEach((value, index) => {
        if (value !== null && value !== undefined) output.push({ time: this.times[index], value });
      });
      return output;
    }

    fill(data) {
      this.data = data;
      this.times = data.candles.map((c) => localTime(c.time));
      const precision = precisionFor(data.candles[data.candles.length - 1]?.close || 1);
      this.digits = precision;
      const format = { type: 'price', precision, minMove: 1 / 10 ** precision };
      this.chart.applyOptions({ localization: { locale: 'pt-BR', priceFormatter: (price) => number(price, Math.abs(price) >= 1000 ? Math.min(precision, 2) : precision) } });
      this.candle.applyOptions({ priceFormat: format });
      this.candle.setData(data.candles.map((c, index) => ({ time: this.times[index], open: c.open, high: c.high, low: c.low, close: c.close })));
      this.volume.setData(data.candles.map((c, index) => ({ time: this.times[index], value: c.volume, color: c.close >= c.open ? '#14532D' : '#7F1D1D' })));
      Object.entries(this.lines).forEach(([key, series]) => {
        if (key === 'macdHistogram') {
          series.setData(this.series(key).map((p) => ({ ...p, color: p.value >= 0 ? '#14532D' : '#7F1D1D' })));
        } else {
          series.setData(this.series(key));
        }
        if (!['rsi', 'macd', 'macdSignal'].includes(key)) series.applyOptions({ priceFormat: format });
      });
      const buys = [];
      (data.buys || []).forEach((buy, index) => {
        if (buy) buys.push({ time: this.times[index], position: 'belowBar', color: '#7CC0FF', shape: 'arrowUp', text: 'C' });
      });
      this.markers.setMarkers(buys);
      this.showLegend();
    }

    update(data) {
      this.fill(data);
      if (!this.fitted) { this.showRecent(); this.fitted = true; }
    }

    reset() { this.fitted = false; }

    showRecent() {
      const total = this.times ? this.times.length : 0;
      if (total) this.chart.timeScale().setVisibleLogicalRange({ from: Math.max(total - 150, 0), to: total + 6 });
    }

    setLevels(levels) {
      const signature = JSON.stringify(levels);
      if (signature === this.levelSignature) return;
      this.levelSignature = signature;
      this.levels = levels;
      this.drawLevels();
    }

    drawLevels() {
      this.levelLines.forEach((line) => { try { this.candle.removePriceLine(line); } catch { /* gráfico recriado */ } });
      this.levelLines = this.levels.filter((level) => level.value !== null && level.value !== undefined).map((level) => this.candle.createPriceLine({
        price: level.value, color: level.color, lineWidth: 1,
        lineStyle: level.dashed ? LC.LineStyle.Dashed : LC.LineStyle.Solid, axisLabelVisible: true, title: level.name,
      }));
    }

    showLegend(time) {
      if (!this.data || !this.data.candles.length) return;
      let index = time === undefined ? -1 : this.times.indexOf(time);
      if (index < 0) index = this.data.candles.length - 1;
      const c = this.data.candles[index], digits = this.digits, change = (c.close / c.open - 1) * 100;
      const pieces = [
        ['strong', this.data.title || ''],
        ['span', `A ${number(c.open, digits)}`], ['span', `M ${number(c.high, digits)}`],
        ['span', `Mi ${number(c.low, digits)}`], ['span', `F ${number(c.close, digits)}`],
        [change >= 0 ? 'up' : 'down', `${change >= 0 ? '+' : ''}${number(change, 2)}%`],
        ['span', `Vol ${compact(c.volume)}`],
      ];
      const indicators = this.data.indicators;
      OVERLAYS.filter((item) => this.choices[item.id] && item.keys.length === 1).forEach((item) => {
        const value = indicators[item.keys[0]][index];
        if (value !== null && value !== undefined) pieces.push(['span', `${item.label} ${number(value, digits)}`]);
      });
      if (this.choices.rsi && indicators.rsi[index] !== null) pieces.push(['span', `RSI ${number(indicators.rsi[index], 1)}`]);
      if (this.choices.macd && indicators.macd[index] !== null) pieces.push(['span', `MACD ${number(indicators.macd[index], digits)}`]);
      this.legend.replaceChildren(...pieces.map(([kind, text]) => {
        const span = document.createElement('span');
        if (kind === 'strong') span.style.fontWeight = '600';
        if (kind === 'up') span.style.color = GREEN;
        if (kind === 'down') span.style.color = RED;
        span.textContent = text;
        return span;
      }));
    }
  }

  window.BotChart = BotChart;
})();
