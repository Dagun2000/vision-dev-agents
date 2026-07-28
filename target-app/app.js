(function () {
  'use strict';

  var STORAGE_KEY = 'simple-todo-list-items';
  var todos = loadTodos();
  var todoForm = document.getElementById('todo-form');
  var todoInput = document.getElementById('todo-input');
  var todoList = document.getElementById('todo-list');
  var emptyMessage = document.getElementById('empty-message');

  function createId() {
    return Date.now() + '-' + Math.random().toString(16).slice(2);
  }

  function loadTodos() {
    var savedTodos;

    try {
      savedTodos = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (Array.isArray(savedTodos)) {
        return savedTodos.reduce(function (validTodos, todo) {
          if (todo && typeof todo.text === 'string' && typeof todo.completed === 'boolean') {
            validTodos.push({
              id: typeof todo.id === 'string' && todo.id ? todo.id : createId(),
              text: todo.text,
              completed: todo.completed
            });
          }
          return validTodos;
        }, []);
      }
    } catch (error) {
      console.warn('저장된 할 일을 불러오지 못했습니다.', error);
    }

    return [];
  }

  function saveTodos() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(todos));
    } catch (error) {
      console.warn('할 일을 저장하지 못했습니다.', error);
    }
  }

  function renderTodos() {
    todoList.innerHTML = '';

    todos.forEach(function (todo) {
      var item = document.createElement('li');
      var label = document.createElement('label');
      var checkbox = document.createElement('input');
      var text = document.createElement('span');
      var checkboxId = 'todo-checkbox-' + todo.id;

      item.className = 'todo-item';
      if (todo.completed) {
        item.className += ' is-completed';
      }

      label.className = 'todo-item-label';
      label.htmlFor = checkboxId;

      checkbox.className = 'todo-checkbox';
      checkbox.id = checkboxId;
      checkbox.type = 'checkbox';
      checkbox.checked = todo.completed;
      checkbox.dataset.todoId = todo.id;
      checkbox.setAttribute('aria-label', todo.text + ' 완료 여부');

      text.className = 'todo-text';
      text.textContent = todo.text;

      label.appendChild(checkbox);
      label.appendChild(text);
      item.appendChild(label);
      todoList.appendChild(item);
    });

    emptyMessage.hidden = todos.length > 0;
  }

  function addTodo(text) {
    todos.push({
      id: createId(),
      text: text,
      completed: false
    });
    saveTodos();
    renderTodos();
  }

  function toggleTodo(id, completed) {
    todos.forEach(function (todo) {
      if (todo.id === id) {
        todo.completed = completed;
      }
    });
    saveTodos();
    renderTodos();
  }

  todoForm.addEventListener('submit', function (event) {
    var text;

    event.preventDefault();
    text = todoInput.placeholder; // [DEBUG BUG INJECTED] 입력값 대신 placeholder 텍스트 사용

    if (!text) {
      todoInput.focus();
      return;
    }

    addTodo(text);
    todoInput.value = '';
    todoInput.focus();
  });

  todoList.addEventListener('change', function (event) {
    var checkbox = event.target;

    if (checkbox.classList.contains('todo-checkbox') && checkbox.dataset.todoId) {
      toggleTodo(checkbox.dataset.todoId, checkbox.checked);
    }
  });

  renderTodos();
}());
