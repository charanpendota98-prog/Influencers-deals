import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'

const scriptUrl = new URL('../dashboard/static/channel-manager.js', import.meta.url)
const storageKey = 'influencer-hub.channel-manager.views.v1'

class Element {
  constructor() {
    this.listeners = {}
    this.children = []
    this.attributes = {}
    this.value = ''
    this.textContent = ''
  }
  addEventListener(name, callback) { this.listeners[name] = callback }
  replaceChildren() { this.children = [] }
  appendChild(child) { this.children.push(child) }
  setAttribute(name, value) { this.attributes[name] = value }
  focus() { this.focused = true }
  click(name = 'click') { return this.listeners[name]?.({ preventDefault() {} }) }
}

async function fixture(search = '?q=worker&status=paused&page=3&size=25') {
  const elements = new Map()
  for (const id of [
    'saved-view-name', 'saved-view-select', 'saved-view-status',
    'saved-view-save', 'saved-view-apply', 'saved-view-delete',
  ]) elements.set(id, new Element())
  const values = new Map()
  const assigned = []
  const document = {
    getElementById: (id) => elements.get(id) || null,
    createElement: () => new Element(),
  }
  const window = {
    localStorage: {
      getItem: (key) => values.get(key) || null,
      setItem: (key, value) => values.set(key, value),
    },
    location: {
      pathname: '/channels',
      search,
      assign: (url) => assigned.push(url),
    },
  }
  const source = await readFile(scriptUrl, 'utf8')
  vm.runInNewContext(source, { document, window, URLSearchParams, Number, JSON, String })
  return { elements, values, assigned }
}

test('saved channel views persist filters, remove pagination, reopen and delete safely', async () => {
  const { elements, values, assigned } = await fixture()
  const name = 'Paused & <img src=x onerror=alert(1)>'
  elements.get('saved-view-name').value = name
  elements.get('saved-view-save').click()

  const saved = JSON.parse(values.get(storageKey))
  assert.equal(saved.length, 1)
  assert.equal(saved[0].name, name)
  assert.equal(saved[0].query, 'q=worker&status=paused&size=25')
  assert.equal(elements.get('saved-view-select').children[1].textContent, name)
  assert.equal(elements.get('saved-view-status').textContent, `Saved “${name}” on this browser.`)

  elements.get('saved-view-select').value = '0'
  elements.get('saved-view-apply').click()
  assert.equal(assigned.length, 1)
  assert.equal(assigned[0], '/channels?q=worker&status=paused&size=25')

  elements.get('saved-view-delete').click()
  assert.deepEqual(JSON.parse(values.get(storageKey)), [])
  assert.equal(elements.get('saved-view-status').textContent, `Removed “${name}”.`)
})

test('empty saved view names are rejected without writing storage', async () => {
  const { elements, values } = await fixture('')
  elements.get('saved-view-save').click()
  assert.equal(values.has(storageKey), false)
  assert.equal(elements.get('saved-view-name').focused, true)
  assert.equal(elements.get('saved-view-status').textContent, 'Enter a name for this view first.')
})
