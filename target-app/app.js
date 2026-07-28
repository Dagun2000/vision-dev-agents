(function () {
  'use strict';

  var STORAGE_KEY = 'todo-items';
  var form = document.getElementById('todo-form');
  var input = document.getElementById('todo-input');
  var list = document.getElementById('todo-list');
  var emptyMessage = document.getElementById('empty-message');

  function getTodos() {
    try {
      var savedTodos = localStorage.getItem(STORAGE_KEY);
      var todos = savedTodos ? JSON.parse(savedTodos) : [];
      return Array.isArray(todos) ? todos : [];
    } catch (error) {
      console.warn('Todo 목록을 불러오지 못했습니다.', error);
      return [];
    }
  }

  function saveTodos(todos) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(todos));
      return true;
    } catch (error) {
      console.warn('Todo 목록을 저장하지 못했습니다.', error);
      return false;
    }
  }

  function getTodoText(todo) {
    if (typeof todo === 'string') {
      return todo;
    }

    return todo && typeof todo.text === 'string' ? todo.text : '';
  }

  function isTodoCompleted(todo) {
    return Boolean(todo && typeof todo === 'object' && todo.completed);
  }

  function setTodoCompletion(index, completed) {
    var todos = getTodos();
    var existingTodo;

    if (!Number.isInteger(index) || index < 0 || index >= todos.length) {
      return;
    }

    existingTodo = todos[index];
    todos[index] = {
      text: getTodoText(existingTodo),
      completed: completed
    };

    saveTodos(todos);
    renderTodos();
  }

  function deleteTodo(index) {
    var todos = getTodos();

    if (!Number.isInteger(index) || index < 0 || index >= todos.length) {
      return;
    }

    todos.splice(index, 1);
    saveTodos(todos);
    renderTodos();
  }

  function renderTodos() {
    var todos = getTodos();

    list.innerHTML = '';

    todos.forEach(function (todo, index) {
      var item = document.createElement('li');
      var checkbox = document.createElement('input');
      var label = document.createElement('label');
      var toggleButton = document.createElement('button');
      var deleteButton = document.createElement('button');
      var todoText = getTodoText(todo);
      var checkboxId = 'todo-check-' + index;
      var completed = isTodoCompleted(todo);

      item.className = 'todo-item' + (completed ? ' completed' : '');
      item.setAttribute('data-index', String(index));

      checkbox.className = 'todo-item-checkbox';
      checkbox.type = 'checkbox';
      checkbox.id = checkboxId;
      checkbox.checked = completed;
      checkbox.setAttribute('data-index', String(index));
      checkbox.setAttribute('aria-label', todoText + ' 완료 여부');

      label.className = 'todo-item-text';
      label.htmlFor = checkboxId;
      label.textContent = todoText;

      toggleButton.className = 'todo-toggle-button';
      toggleButton.type = 'button';
      toggleButton.textContent = completed ? '완료 취소' : '완료';
      toggleButton.setAttribute('data-index', String(index));
      toggleButton.setAttribute('aria-label', todoText + (completed ? ' 완료 취소' : ' 완료'));

      deleteButton.className = 'todo-delete-button';
      deleteButton.type = 'button';
      deleteButton.textContent = '삭제';
      deleteButton.setAttribute('data-index', String(index));
      deleteButton.setAttribute('aria-label', todoText + ' 삭제');

      item.appendChild(checkbox);
      item.appendChild(label);
      item.appendChild(toggleButton);
      item.appendChild(deleteButton);
      list.appendChild(item);
    });

    emptyMessage.hidden = todos.length > 0;
  }

  form.addEventListener('submit', function (event) {
    var newTodo = input.value.trim();
    var todos;

    event.preventDefault();

    if (!newTodo) {
      input.focus();
      return;
    }

    todos = getTodos();
    todos.push({
      text: newTodo,
      completed: false
    });
    saveTodos(todos);
    renderTodos();
    input.value = '';
    input.focus();
  });

  list.addEventListener('change', function (event) {
    var checkbox = event.target;
    var index;

    if (!checkbox.matches('.todo-item-checkbox')) {
      return;
    }

    index = Number(checkbox.getAttribute('data-index'));
    setTodoCompletion(index, checkbox.checked);
  });

  list.addEventListener('click', function (event) {
    var target = event.target;
    var index;
    var todos;

    if (target.matches('.todo-delete-button')) {
      index = Number(target.getAttribute('data-index'));
      deleteTodo(index);
      return;
    }

    if (target.matches('.todo-toggle-button')) {
      index = Number(target.getAttribute('data-index'));
      todos = getTodos();

      if (!Number.isInteger(index) || index < 0 || index >= todos.length) {
        return;
      }

      setTodoCompletion(index, !isTodoCompleted(todos[index]));
      return;
    }

    if (!target.matches('.todo-item')) {
      return;
    }

    index = Number(target.getAttribute('data-index'));
    todos = getTodos();

    if (!Number.isInteger(index) || index < 0 || index >= todos.length) {
      return;
    }

    setTodoCompletion(index, !isTodoCompleted(todos[index]));
  });

  renderTodos();
}());
