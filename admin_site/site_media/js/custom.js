// Set up global BibOS instance used for accessing utility methods
var BibOS;
(function($) {
  function BibOS(args) {
    this.templates = {};
    this.documentReady = false
    this.loadedItems = {}
    this.scriptsToLoad = []
    this.cssToLoad = []
  }

  $.extend(BibOS.prototype, {
    init: function() {
    },

    onDOMReady: function() {
      var t = this;
      t.documentReady = true;

      // Mark what we have already loaded
      $('script').each(function() {
        t.loadedItems[$(this).attr('src') || ''] = true
      })
      $.each(this.scriptsToLoad, function() {
        t.loadScript(this)
      })
      $('link').each(function() {
        t.loadedItems[$(this).attr('href') || ''] = true
      })
      $.each(this.cssToLoad, function() {
        t.loadStylesheet(this)
      })
    },

    loadResource: function(type, src) {
      if(this.documentReady) {
        var item;
        if(this.loadedItems[src])
          return
        if(type == 'css' || type == 'stylesheet') {
          var css = $('<link>', {
            'href': src,
            'type': 'text/css',
            'rel': 'stylesheet'
          });
          css.appendTo($('head'));
        } else if(type == 'script' || type == 'javascript') {
          var script = $('<script>', {'src': src, 'type':'text/javascript'});
          script.appendTo($('body'));
        } else {
          alert("Don't know how to load item of type " + type);
          return
        }
        this.loadedItems[src] = true;
      } else {
        if (type == 'css' || type == 'stylesheet') {
          this.cssToLoad.push(src)
        } else if(type == 'script') {
          this.scriptsToLoad.push(src)
        } else {
          alert("Don't know how to load item of type " + type + ' once')
        }
      }
    },

    loadScript: function(src) {
      this.loadResource('script', src);
    },
    
    loadStylesheet: function(src) {
      this.loadResource('css', src);
    },

    translate: function() {
      // TODO: implement actual translation, this is just poor man's sprintf
      var args = arguments, arg_idx = 1, key = arguments[0] || '';
      if (arguments.length > 1) {
        key = key.replace(/\%s/g, function(m) {
          var v = args[arg_idx++]; 
          return v == undefined ? '' : v;
        })
      }
      return key
    },
    
    // Load a template from innerHTML of element specified by id
    addTemplate: function(name, id) {
      this.templates[name] = $(id).html()
    },

    // Expand a template with the given data
    expandTemplate: function(templateName, data) {
      var html = this.templates[templateName] || '';
      var expander = function(fullmatch, key) {
          k = key.toLowerCase()
          return k in data ? data[k] : fullmatch;
      }
      html = html.replace(/<!--#([^#]+)#-->/g, expander);
      return html.replace(/#([^#]+)#/g, expander);
    },

    getCookie: function (name) {
      var cookieValue = null;
      if (document.cookie && document.cookie != '') {
          var cookies = document.cookie.split(';');
          for (var i = 0; i < cookies.length; i++) {
              var cookie = jQuery.trim(cookies[i]);
              // Does this cookie string begin with the name we want?
              if (cookie.substring(0, name.length + 1) == (name + '=')) {
                  cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                  break;
              }
          }
      }
      return cookieValue;
    },
    csrfSafeMethod: function (method) {
        // these HTTP methods do not require CSRF protection
        return (/^(GET|HEAD|OPTIONS|TRACE)$/.test(method));
    },
    sameOrigin: function (url) {
        // test that a given url is a same-origin URL
        // url could be relative or scheme relative or absolute
        var host = document.location.host; // host + port
        var protocol = document.location.protocol;
        var sr_origin = '//' + host;
        var origin = protocol + sr_origin;
        // Allow absolute or scheme relative URLs to same origin
        return (url == origin || url.slice(0, origin.length + 1) == origin + '/') ||
            (url == sr_origin || url.slice(0, sr_origin.length + 1) == sr_origin + '/') ||
            // or any other URL that isn't scheme relative or absolute i.e relative.
            !(/^(\/\/|http:|https:).*/.test(url));
    },
    getOrderBy: function(old_order, new_order) {
      var desc = false,
          old_desc = false;
      if (old_order.match(/^\-/)) {
          old_order = old_order.replace(/^\-/, '')
          old_desc = true;
      }
      if (new_order == old_order)
          desc = !old_desc;
      return (desc ? '-' : '') + new_order;
    },
    setOrderByClasses: function(elem, list, orderkey) {
      $(list).removeClass('orderby').removeClass('orderby-desc')
      $(elem).addClass(orderkey.match(/^-/) ? 'orderby-desc' : 'orderby');
    }
  })
  window.BibOS = window.BibOS || new BibOS();
  var b = window.BibOS;
  b.init();
  $(function() { b.onDOMReady() })

  // Setup support for CSRFToken in ajax calls
  $.ajaxSetup({
    beforeSend: function(xhr, settings) {
      if (!b.csrfSafeMethod(settings.type) && b.sameOrigin(settings.url)) {
        // Send the token to same-origin, relative URLs only.
        // Send the token only if the method warrants CSRF protection
        // Using the CSRFToken value acquired earlier
        xhr.setRequestHeader("X-CSRFToken", b.getCookie('csrftoken'));
      }
    }
  });

})($)

