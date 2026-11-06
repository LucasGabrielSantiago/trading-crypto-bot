/* Ferramentas de desenho do gráfico. A biblioteca não traz desenhos, então eles ficam em uma camada SVG. */
(() => {
  const NS = 'http://www.w3.org/2000/svg';
  const FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  const ICONS = {
    trend: '<path d="M6.6 21.4 21.4 6.6"/><circle cx="5" cy="23" r="2.2"/><circle cx="23" cy="5" r="2.2"/>',
    horizontal: '<path d="M3 14h8.8M16.2 14H25"/><circle cx="14" cy="14" r="2.2"/>',
    rect: '<path d="M8.2 6h11.6M8.2 22h11.6M6 8.2v11.6M22 8.2v11.6"/><circle cx="6" cy="6" r="2.2"/><circle cx="22" cy="6" r="2.2"/><circle cx="6" cy="22" r="2.2"/><circle cx="22" cy="22" r="2.2"/>',
    fib: '<path d="M4 6h20M4 11h20M4 17h20M4 22h20"/><circle cx="8" cy="6" r="1.8" fill="#131722"/><circle cx="20" cy="22" r="1.8" fill="#131722"/>',
    text: '<path d="M7 8V6h14v2M14 6v16M11 22h6"/>',
    ruler: '<path d="m5 20 15-15 3 3-15 15z"/><path d="m9.5 15.5 2 2M13 12l2 2M16.5 8.5l2 2"/>',
    magnet: '<path d="M7 6h4v9a3 3 0 0 0 6 0V6h4v9a7 7 0 0 1-14 0zM7 10h4M17 10h4"/>',
    lock: '<rect x="7" y="13" width="14" height="10" rx="1.5"/><path d="M10 13V9.5a4 4 0 0 1 8 0V13M14 17v2"/>',
    eye: '<path d="M3.5 14S7.5 7.5 14 7.5 24.5 14 24.5 14 20.5 20.5 14 20.5 3.5 14 3.5 14z"/><circle cx="14" cy="14" r="3"/>',
    long: '<path d="M5 5v18h18"/><path d="m9 18 5-5 3 3 6-7M18.5 9H23v4.5"/>',
    short: '<path d="M5 5v18h18"/><path d="m9 9 5 5 3-3 6 7M18.5 18H23v-4.5"/>',
    trash: '<path d="M6 8h16M11 8V5.5h6V8M8 8l1 15h10l1-15M12 12v7M16 12v7"/>',
  };
  const number = (value, digits) => Number(value).toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits });

  class ChartDrawings {
    constructor(owner) {
      this.owner = owner;
      this.items = [];
      this.tool = null;
      this.first = null;
      this.second = null;
      this.hint = '';
      this.pointer = null;
      this.ruler = null;
      this.selected = null;
      this.magnet = false;
      this.locked = false;
      this.hidden = false;
      this.symbol = null;
      this.frame = 0;
      this.layer = document.createElementNS(NS, 'svg');
      this.layer.setAttribute('class', 'draw-layer');
      this.layer.setAttribute('aria-hidden', 'true');
      this.layer.addEventListener('pointerdown', (event) => this.click(event));
      this.layer.addEventListener('pointermove', (event) => this.move(event));
      this.buildToolbar();
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && (this.tool || this.first)) this.setTool(null);
        const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || '');
        if ((event.key === 'Delete' || event.key === 'Backspace') && this.selected && !typing) {
          event.preventDefault();
          this.remove(this.selected);
        }
      });
      owner.surface.addEventListener('pointermove', () => this.schedule());
      owner.surface.addEventListener('wheel', () => this.schedule(), { passive: true });
    }

    button(id, label, action, toggle = true) {
      const button = document.createElement('button');
      button.className = 'draw-tool';
      button.dataset.tip = label;
      button.dataset.tool = id;
      button.setAttribute('aria-label', label);
      if (toggle) button.setAttribute('aria-pressed', 'false');
      button.innerHTML = `<svg width="24" height="24" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round" style="width:24px;height:24px" aria-hidden="true">${ICONS[id]}</svg>`;
      button.addEventListener('click', action);
      return button;
    }

    buildToolbar() {
      this.toolbar = document.createElement('div');
      this.toolbar.className = 'draw-tools';
      this.toolbar.setAttribute('role', 'toolbar');
      this.toolbar.setAttribute('aria-label', 'Ferramentas de desenho');
      this.buttons = {};
      const add = (id, label, action, toggle) => {
        this.buttons[id] = this.button(id, label, action, toggle);
        this.toolbar.appendChild(this.buttons[id]);
      };
      const tool = (id) => () => this.setTool(this.tool === id ? null : id);
      add('trend', 'Linha de tendência', tool('trend'));
      add('horizontal', 'Linha horizontal', tool('horizontal'));
      add('rect', 'Retângulo', tool('rect'));
      add('fib', 'Fibonacci', tool('fib'));
      add('text', 'Texto', tool('text'));
      add('long', 'Posição long', tool('long'));
      add('short', 'Posição short', tool('short'));
      add('ruler', 'Régua', tool('ruler'));
      const line = document.createElement('div');
      line.className = 'draw-sep';
      this.toolbar.appendChild(line);
      add('magnet', 'Ímã', () => this.flag('magnet'));
      add('lock', 'Travar desenhos', () => this.flag('locked', 'lock'));
      add('eye', 'Ocultar desenhos', () => this.flag('hidden', 'eye'));
      add('trash', 'Apagar selecionado (sem seleção, todos)', () => this.erase(), false);
    }

    flag(name, id = name) {
      this[name] = !this[name];
      this.buttons[id].setAttribute('aria-pressed', String(this[name]));
      this.schedule();
    }

    setTool(tool) {
      this.tool = tool;
      this.first = null;
      this.second = null;
      this.hint = '';
      this.pointer = null;
      if (tool !== 'ruler') this.ruler = null;
      ['trend', 'horizontal', 'rect', 'fib', 'text', 'long', 'short', 'ruler'].forEach((id) => {
        this.buttons[id].setAttribute('aria-pressed', String(id === tool));
      });
      this.layer.classList.toggle('drawing', Boolean(tool));
      this.schedule();
    }

    storageKey() {
      return `bot-drawings-${this.owner.host.id}-${this.symbol}`;
    }

    setSymbol(symbol) {
      if (symbol === this.symbol) return;
      this.symbol = symbol;
      this.selected = null;
      try { this.items = JSON.parse(localStorage.getItem(this.storageKey())) || []; } catch { this.items = []; }
    }

    save() {
      try { localStorage.setItem(this.storageKey(), JSON.stringify(this.items)); } catch { /* opcional */ }
    }

    remove(id) {
      if (this.locked) return;
      this.items = this.items.filter((item) => item.id !== id);
      if (this.selected === id) this.selected = null;
      this.save();
      this.schedule();
    }

    erase() {
      if (this.locked) return;
      if (this.selected) {
        this.remove(this.selected);
        return;
      }
      if (this.items.length && window.confirm('Apagar todos os desenhos deste par?')) {
        this.items = [];
      }
      this.ruler = null;
      this.save();
      this.schedule();
    }

    point(event) {
      const owner = this.owner;
      const bounds = this.layer.getBoundingClientRect();
      const x = event.clientX - bounds.left, y = event.clientY - bounds.top;
      const scale = owner.chart.timeScale();
      const logical = Math.round(scale.coordinateToLogical(x));
      const index = Math.max(0, Math.min(owner.times.length - 1, logical));
      let price = owner.candle.coordinateToPrice(y);
      if (this.magnet && owner.data.candles[index]) {
        const candle = owner.data.candles[index];
        price = [candle.open, candle.high, candle.low, candle.close].reduce((best, value) => (
          Math.abs(value - price) < Math.abs(best - price) ? value : best));
      }
      return { time: owner.times[index], price };
    }

    click(event) {
      if (!this.tool || !this.owner.times || !this.owner.times.length) return;
      event.preventDefault();
      const point = this.point(event), tool = this.tool;
      if (tool === 'horizontal') return this.add({ type: tool, a: point });
      if (tool === 'text') {
        const text = window.prompt('Texto da anotação:');
        return text ? this.add({ type: tool, a: point, text: text.slice(0, 80) }) : this.setTool(null);
      }
      if (tool === 'long' || tool === 'short') return this.clickPosition(point, tool);
      if (!this.first) { this.first = point; this.pointer = point; return this.schedule(); }
      if (tool === 'ruler') {
        this.ruler = { type: tool, a: this.first, b: point };
        this.first = null;
        return this.setTool('ruler');
      }
      return this.add({ type: tool, a: this.first, b: point });
    }

    clickPosition(point, tool) {
      const long = tool === 'long';
      this.hint = '';
      if (!this.first) {
        this.first = point;
        this.pointer = point;
      } else if (!this.second) {
        if (long ? point.price >= this.first.price : point.price <= this.first.price) {
          this.hint = long ? 'No long, o stop fica abaixo da entrada' : 'No short, o stop fica acima da entrada';
        } else {
          this.second = point;
        }
      } else if (long ? point.price <= this.first.price : point.price >= this.first.price) {
        this.hint = long ? 'No long, o alvo fica acima da entrada' : 'No short, o alvo fica abaixo da entrada';
      } else {
        return this.add({ type: tool, a: this.first, b: this.second, c: point });
      }
      return this.schedule();
    }

    move(event) {
      if (!this.first) return;
      this.pointer = this.point(event);
      this.schedule();
    }

    add(item) {
      this.items.push({ ...item, id: `${Date.now()}-${this.items.length}` });
      this.save();
      this.setTool(null);
    }

    index(time) {
      // Procura a vela mais próxima: o desenho continua no lugar ao trocar o tempo gráfico.
      const times = this.owner.times;
      if (!times.length) return -1;
      let low = 0, high = times.length - 1;
      while (low < high) {
        const middle = (low + high) >> 1;
        if (times[middle] < time) low = middle + 1; else high = middle;
      }
      return low > 0 && time - times[low - 1] < times[low] - time ? low - 1 : low;
    }

    x(time) {
      const index = this.index(time);
      return index < 0 ? null : this.owner.chart.timeScale().logicalToCoordinate(index);
    }

    y(price) {
      return this.owner.candle.priceToCoordinate(price);
    }

    schedule() {
      if (this.frame) return;
      this.frame = requestAnimationFrame(() => { this.frame = 0; this.draw(); });
    }

    element(parent, tag, attributes, text) {
      const node = document.createElementNS(NS, tag);
      Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
      if (text !== undefined) node.textContent = text;
      parent.appendChild(node);
      return node;
    }

    shape(parent, item, width, height, color) {
      const owner = this.owner, digits = owner.digits;
      const ax = this.x(item.a.time), ay = this.y(item.a.price);
      const bx = item.b ? this.x(item.b.time) : null, by = item.b ? this.y(item.b.price) : null;
      if (ay === null || (item.type !== 'horizontal' && ax === null)) return;
      if (item.type === 'long' || item.type === 'short') {
        this.position(parent, item, ax, ay, bx, by, color);
        return;
      }
      const stroke = { stroke: color, 'stroke-width': 1.5 };
      if (item.type === 'horizontal') {
        this.element(parent, 'line', { x1: 0, x2: width, y1: ay, y2: ay, ...stroke });
        this.element(parent, 'text', { x: 8, y: ay - 5, fill: color, 'font-size': 12 }, number(item.a.price, digits));
      } else if (item.type === 'text') {
        this.element(parent, 'text', { x: ax + 4, y: ay, fill: color, 'font-size': 13 }, item.text);
      } else if (bx !== null && by !== null) {
        if (item.type === 'trend') this.element(parent, 'line', { x1: ax, y1: ay, x2: bx, y2: by, ...stroke });
        if (item.type === 'rect') {
          this.element(parent, 'rect', {
            x: Math.min(ax, bx), y: Math.min(ay, by), width: Math.abs(bx - ax), height: Math.abs(by - ay),
            fill: 'rgba(91,156,246,.12)', ...stroke,
          });
        }
        if (item.type === 'fib') {
          FIB.forEach((level) => {
            const price = item.a.price + (item.b.price - item.a.price) * level, y = this.y(price);
            if (y === null) return;
            this.element(parent, 'line', { x1: Math.min(ax, bx), x2: Math.max(ax, bx), y1: y, y2: y, ...stroke, 'stroke-width': 1 });
            this.element(parent, 'text', { x: Math.min(ax, bx) + 4, y: y - 3, fill: color, 'font-size': 11 }, `${number(level * 100, 1)}%  ${number(price, digits)}`);
          });
        }
        if (item.type === 'ruler') {
          const change = (item.b.price / item.a.price - 1) * 100;
          const bars = Math.abs(this.index(item.b.time) - this.index(item.a.time));
          const tone = change >= 0 ? '#22C55E' : '#EF4444';
          this.element(parent, 'rect', {
            x: Math.min(ax, bx), y: Math.min(ay, by), width: Math.abs(bx - ax), height: Math.abs(by - ay),
            fill: change >= 0 ? 'rgba(34,197,94,.14)' : 'rgba(239,68,68,.14)', stroke: tone,
          });
          this.element(parent, 'text', { x: bx + 6, y: by, fill: tone, 'font-size': 12 },
            `${change >= 0 ? '+' : ''}${number(change, 2)}%  ${number(item.b.price - item.a.price, digits)}  ${bars} velas`);
        }
      }
    }

    position(parent, item, ax, ay, bx, by, color) {
      const digits = this.owner.digits, entry = item.a.price;
      const cy = item.c ? this.y(item.c.price) : null;
      const cx = item.c ? this.x(item.c.time) : null;
      const right = Math.max(ax + 110, bx ?? 0, cx ?? 0);
      const zone = (from, to, fill, stroke) => this.element(parent, 'rect', {
        x: ax, width: right - ax, y: Math.min(from, to), height: Math.abs(to - from), fill, stroke, 'stroke-width': 1,
      });
      const label = (y, text, fill) => this.element(parent, 'text', { x: ax + 6, y, fill, 'font-size': 12 }, text);
      const percent = (price) => Math.abs(price / entry - 1) * 100;
      if (by !== null) zone(ay, by, 'rgba(239,68,68,.20)', '#EF4444');
      if (cy !== null) zone(ay, cy, 'rgba(34,197,94,.20)', '#22C55E');
      this.element(parent, 'line', { x1: ax, x2: right, y1: ay, y2: ay, stroke: color, 'stroke-width': 1.5 });
      const name = item.type === 'long' ? 'Long' : 'Short';
      if (by !== null) label(by + (by > ay ? -5 : 14), `Stop ${number(item.b.price, digits)} (${number(percent(item.b.price), 2)}%)`, '#FCA5A5');
      if (cy !== null) {
        label(cy + (cy > ay ? -5 : 14), `Alvo ${number(item.c.price, digits)} (${number(percent(item.c.price), 2)}%)`, '#86EFAC');
        const risk = Math.abs(entry - item.b.price), reward = Math.abs(item.c.price - entry);
        label(ay - 5, `${name} ${number(entry, digits)}  Risco/retorno 1:${number(reward / risk, 2)}`, '#E8EDF2');
      } else {
        label(ay - 5, `${name} ${number(entry, digits)}`, '#E8EDF2');
      }
    }

    draw() {
      const owner = this.owner;
      if (!owner.chart || !owner.times) return;
      const width = owner.chart.timeScale().width(), height = owner.chart.panes()[0].getHeight();
      this.layer.setAttribute('width', width);
      this.layer.setAttribute('height', height);
      this.layer.setAttribute('viewBox', `0 0 ${width} ${height}`);
      // A folha de estilos da página estica todo SVG; aqui a escala precisa ser 1:1 com o gráfico.
      this.layer.style.width = `${width}px`;
      this.layer.style.height = `${height}px`;
      this.layer.replaceChildren();
      if (!this.hidden) {
        this.items.forEach((item) => {
          const group = this.element(this.layer, 'g', { class: 'draw-hit' });
          this.shape(group, item, width, height, item.id === this.selected ? '#FFB454' : '#5B9CF6');
          this.hitArea(group, item, width);
        });
      }
      if (this.ruler) this.shape(this.layer, this.ruler, width, height, '#E8EDF2');
      if (this.first && this.pointer) {
        const preview = this.second
          ? { type: this.tool, a: this.first, b: this.second, c: this.pointer }
          : { type: this.tool, a: this.first, b: this.pointer };
        this.shape(this.layer, preview, width, height, '#E8EDF2');
      }
      const guide = this.hint || this.nextStep();
      if (guide) this.element(this.layer, 'text', { x: 10, y: height - 12, fill: '#FFB454', 'font-size': 13 }, guide);
    }

    nextStep() {
      if (this.tool !== 'long' && this.tool !== 'short') return '';
      if (!this.first) return 'Clique no preço de entrada';
      return this.second ? 'Clique no preço do alvo' : 'Clique no preço do stop';
    }

    anchor(item, width) {
      if (item.type === 'horizontal') return [width - 22, this.y(item.a.price)];
      const points = [item.a, item.b, item.c].filter(Boolean);
      const xs = points.map((p) => this.x(p.time)).filter((v) => v !== null);
      const ys = points.map((p) => this.y(p.price)).filter((v) => v !== null);
      if (!xs.length || !ys.length) return null;
      const extra = item.type === 'long' || item.type === 'short' ? Math.max(0, xs[0] + 110 - Math.max(...xs)) : 0;
      return [Math.min(width - 14, Math.max(...xs) + extra), Math.max(14, Math.min(...ys))];
    }

    hitArea(group, item, width) {
      const active = !this.tool;
      group.style.pointerEvents = active ? 'auto' : 'none';
      // Linhas finas ganham uma área invisível maior para facilitar o clique.
      [...group.querySelectorAll('line')].forEach((line) => {
        const clone = line.cloneNode();
        clone.setAttribute('stroke', 'transparent');
        clone.setAttribute('stroke-width', 12);
        group.appendChild(clone);
      });
      group.addEventListener('pointerdown', (event) => {
        event.stopPropagation();
        this.selected = this.selected === item.id ? null : item.id;
        this.schedule();
      });
      if (item.id !== this.selected || this.locked) return;
      const spot = this.anchor(item, width);
      if (!spot) return;
      const [x, y] = spot;
      const close = this.element(this.layer, 'g', { class: 'draw-delete', role: 'button', 'aria-label': 'Excluir desenho' });
      close.style.pointerEvents = 'auto';
      close.style.cursor = 'pointer';
      this.element(close, 'circle', { cx: x, cy: y, r: 11, fill: '#EF4444' });
      this.element(close, 'path', { d: `M${x - 4} ${y - 4}l8 8M${x + 4} ${y - 4}l-8 8`, stroke: '#fff', 'stroke-width': 2 });
      close.addEventListener('pointerdown', (event) => {
        event.stopPropagation();
        this.remove(item.id);
      });
    }
  }

  window.ChartDrawings = ChartDrawings;
})();
