"""
URL configuration for campuspilot project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from myapp import views

# These read-only routes expose extracted notices and their actionable details to the website.
urlpatterns = [
    path('admin/', admin.site.urls),
    # Serve the dashboard and notice pages alongside the existing JSON API.
    path('', views.dashboard_page, name='dashboard'),
    # Use Django's account flow so personal choices can be tied to an authenticated student.
    path(
        'accounts/login/',
        auth_views.LoginView.as_view(template_name='myapp/login.html'),
        name='login',
    ),
    path(
        'accounts/logout/',
        auth_views.LogoutView.as_view(),
        name='logout',
    ),
    path('accounts/signup/', views.sign_up, name='signup'),
    # Personal status changes are POST-only and never modify shared notice content.
    path('actions/<int:action_item_id>/complete/', views.complete_action, name='complete-action'),
    path('actions/<int:action_item_id>/restore/', views.restore_action, name='restore-action'),
    path('dates/<int:date_id>/dismiss/', views.dismiss_date, name='dismiss-date'),
    path('dates/<int:date_id>/restore/', views.restore_date, name='restore-date'),
    path(
        'plan/actions/<int:action_item_id>/save/',
        views.save_action_plan_item,
        name='save-action-plan-item',
    ),
    path(
        'plan/actions/<int:action_item_id>/dismiss/',
        views.dismiss_action_plan_item,
        name='dismiss-action-plan-item',
    ),
    path('plan/add/', views.add_plan_item, name='add-plan-item'),
    path(
        'plan/items/<int:plan_item_id>/save/',
        views.save_plan_item,
        name='save-plan-item',
    ),
    path(
        'plan/items/<int:plan_item_id>/remove/',
        views.remove_plan_item,
        name='remove-plan-item',
    ),
    path('documents/', views.document_list_page, name='document-list-page'),
    path('announcements/upload/', views.announcement_upload_page, name='announcement-upload'),
    path(
        'announcements/<int:upload_id>/retry/',
        views.retry_announcement_upload,
        name='retry-announcement-upload',
    ),
    path(
        'documents/<int:document_id>/',
        views.document_detail_page,
        name='document-detail-page',
    ),
    path('api/documents/', views.document_list, name='document-list'),
    path('api/deadlines/', views.deadline_list, name='deadline-list'),
    path(
        'api/documents/<int:document_id>/',
        views.document_detail,
        name='document-detail',
    ),
]
