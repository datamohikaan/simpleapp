$(document).ready(function () {
  $(function () {
    console.log("hello js");
    let url = new URL(window.location.href);
    if (!(["/over"].includes(url.pathname))){
        console.log('We are not in over, we are in: ' + url.pathname);
        window.history.replaceState(null, "", url.pathname);
    }
    console.log("we zijn in document ready van mb-functies en dit is usermenu: " + usermenu);
    })
  });

  // hoi