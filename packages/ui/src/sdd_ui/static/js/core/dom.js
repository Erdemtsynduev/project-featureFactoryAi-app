/* Small DOM toolkit. `h` builds elements declaratively; text is always set as
 * text, never parsed as HTML, so user and agent content cannot inject markup. */

export const byId = (id) => document.getElementById(id);

/**
 * h("button", { class: "primary", onclick }, "Save")
 * Attributes: `class`, `dataset`, `style` (object), `on*` handlers, booleans
 * (`hidden`, `disabled`, ...) and plain attributes. Children may be nodes,
 * strings, numbers, arrays or null/false (skipped).
 */
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style")
      for (const [prop, v] of Object.entries(value))
        node.style.setProperty(prop, v);
    else if (key.startsWith("on") && typeof value === "function")
      node.addEventListener(key.slice(2), value);
    else if (key in node && typeof value === "boolean") node[key] = value;
    else if (key === "value" && "value" in node) node.value = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

export function replace(node, ...children) {
  node.replaceChildren();
  return append(node, children);
}

const SVG = "http://www.w3.org/2000/svg";
export function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs))
    if (value !== undefined && value !== null) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Form values as an object; checkboxes become booleans. */
export function formValues(form) {
  const values = {};
  for (const field of form.elements) {
    if (!field.name || field.disabled) continue;
    if (field.type === "checkbox") values[field.name] = field.checked;
    else if (field.type === "radio") {
      if (field.checked) values[field.name] = field.value;
    } else values[field.name] = field.value;
  }
  return values;
}

export function fillForm(form, values) {
  for (const field of form.elements) {
    if (!field.name || !(field.name in values)) continue;
    if (field.type === "checkbox") field.checked = !!values[field.name];
    else if (field.type === "radio")
      field.checked = field.value === values[field.name];
    else field.value = values[field.name] ?? "";
  }
}

/** Render only when the signature of the inputs changed. */
export function memo() {
  let last;
  return (inputs, render) => {
    const signature = JSON.stringify(inputs);
    if (signature === last) return false;
    last = signature;
    render();
    return true;
  };
}
