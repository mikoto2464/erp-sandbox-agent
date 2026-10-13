import { createRouter, createWebHashHistory } from 'vue-router';
import ProjectsPage from '../views/ProjectsPage.vue';
import WorkflowPage from '../views/WorkflowPage.vue';
import TemplateLibraryPage from '../views/TemplateLibraryPage.vue';
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/projects' },
    { path: '/projects', name: 'projects', component: ProjectsPage },
    { path: '/templates', name: 'templates', component: TemplateLibraryPage },
    {
      path: '/projects/:projectId([a-f0-9]{32})/steps/:step([1-5])',
      name: 'workflow',
      component: WorkflowPage,
    },
    { path: '/:pathMatch(.*)*', redirect: '/projects' },
  ],
});
