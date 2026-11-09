import { createApp } from "vue";
import { ElIcon, ElProgress } from "element-plus";
import "element-plus/dist/index.css";
import "./style.css";
import App from "./App.vue";
createApp(App)
  .component("ElIcon", ElIcon)
  .component("ElProgress", ElProgress)
  .mount("#app");
