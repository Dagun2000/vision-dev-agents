(function () {
  'use strict';

  var todos = [];
  var todoForm = document.getElementById('todo-form');
  var todoInput = document.getElementById('todo-input');
  var todoList = document.getElementById('todo-list');
  var emptyMessage = document.getElementById('empty-message');

  function renderTodos() {
    todoList.innerHTML = '';

    todos.forEach(function (todo) {
      var item = document.createElement('li');
      item.className = 'todo-item';
      item.textContent = todo.text;
      todoList.appendChild(item);
    });

    emptyMessage.hidden = todos.length > 0;
  }

  function addTodo(text) {
    todos.push({
      id: Date.now(),
      text: text
    });
    renderTodos();
  }

  todoForm.addEventListener('submit', function (event) {
    event.preventDefault();

    var text = todoInput.value.trim();
    if (!text) {
      todoInput.focus();
      return;
    }

    addTodo(text);
    todoInput.value = '';
    todoInput.focus();
  });

  renderTodos();
}());
