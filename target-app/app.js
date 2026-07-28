(function () {
  'use strict';

  var STORAGE_KEY = 'todo-list-items';
  var form = document.getElementById('todo-form');
  var input = document.getElementById('todo-input');
  var list = document.getElementById('todo-list');
  var warning = document.getElementById('input-warning');
  var warningTimer;
  var todos = loadTodos();
  var nextId = Date.now();

  function loadTodos() {
    var savedTodos;
    var parsedTodos;

    try {
      savedTodos = window.localStorage.getItem(STORAGE_KEY);
      parsedTodos = savedTodos ? JSON.parse(savedTodos) : [];
    } catch (error) {
      return [];
    }

    if (!Array.isArray(parsedTodos)) {
      return [];
    }

    return parsedTodos.filter(function (todo) {
      return todo && typeof todo.text === 'string';
    }).map(function (todo) {
      return {
        id: typeof todo.id === 'string' ? todo.id : String(nextId++),
        text: todo.text,
        completed: todo.completed === true,
        deleted: todo.deleted === true
      };
    });
  }

  function saveTodos() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(todos));
    } catch (error) {
      console.warn('Todo 목록을 저장하지 못했습니다.', error);
    }
  }

  function showWarning() {
    window.clearTimeout(warningTimer);
    warning.hidden = false;
    warningTimer = window.setTimeout(function () {
      warning.hidden = true;
    }, 2000);
  }

  function setCompleted(item, checkbox, completed) {
    checkbox.checked = completed;
    item.classList.toggle('is-completed', completed);
  }

  function createTodoItem(todo) {
    var item = document.createElement('li');
    var checkbox = document.createElement('input');
    var content = document.createElement('span');
    var deleteButton = document.createElement('button');

    item.className = 'todo-item';

    checkbox.className = 'todo-checkbox';
    checkbox.type = 'checkbox';
    checkbox.setAttribute('aria-label', '완료 표시');
    checkbox.addEventListener('click', function (event) {
      event.stopPropagation();
    });
    checkbox.addEventListener('change', function () {
      todo.completed = checkbox.checked;
      setCompleted(item, checkbox, todo.completed);
      saveTodos();
    });

    content.className = 'todo-text';
    content.textContent = todo.text;

    deleteButton.className = 'delete-button';
    deleteButton.type = 'button';
    deleteButton.textContent = '삭제';
    deleteButton.setAttribute('aria-label', todo.text + ' 삭제');
    deleteButton.addEventListener('click', function (event) {
      event.stopPropagation();
      todo.deleted = true;
      item.remove();
      saveTodos();
    });

    item.addEventListener('click', function () {
      todo.completed = !checkbox.checked;
      setCompleted(item, checkbox, todo.completed);
      saveTodos();
    });

    item.appendChild(checkbox);
    item.appendChild(content);
    item.appendChild(deleteButton);
    setCompleted(item, checkbox, todo.completed);
    return item;
  }

  function restoreTodos() {
    todos.forEach(function (todo) {
      if (!todo.deleted) {
        list.appendChild(createTodoItem(todo));
      }
    });
  }

  form.addEventListener('submit', function (event) {
    var text;
    var todo;

    event.preventDefault();
    text = input.value.trim();

    if (!text) {
      showWarning();
      input.focus();
      return;
    }

    todo = {
      id: String(nextId++),
      text: text,
      completed: false,
      deleted: false
    };
    todos.push(todo);
    list.appendChild(createTodoItem(todo));
    saveTodos();
    input.value = '';
    warning.hidden = true;
    window.clearTimeout(warningTimer);
    input.focus();
  });

  restoreTodos();
}());
