/* eslint-disable */
/**
 * AUTO-GENERATED, DO NOT EDIT.
 *
 * Source: config/scenarios.yaml
 * Regenerate: npm run scenarios:gen
 */
import type { ScenarioCatalog } from './scenarioTypes'

export const SCENARIO_CATALOG: ScenarioCatalog = {
  "version": 1,
  "defaultCount": 2,
  "customScenario": {
    "enabled": true,
    "maxLength": 200
  },
  "categories": [
    {
      "id": "booking",
      "titleZh": "预订/咨询服务",
      "scenarios": [
        {
          "key": "booking-hotel",
          "titleZh": "酒店预订",
          "hint": "A traveller phones a hotel to book a room. The receptionist supplies room types, nightly rates, available dates, included facilities, and the booking reference."
        },
        {
          "key": "booking-car-rental",
          "titleZh": "租车咨询",
          "hint": "A customer enquires about renting a car. The agent covers vehicle categories, daily price, driver age and licence requirements, insurance options, and pickup times."
        },
        {
          "key": "booking-shipping",
          "titleZh": "货运寄送",
          "hint": "A student asks a shipping company about sending belongings abroad. The clerk gives weight limits, prohibited items, packaging rules, collection dates, and total cost."
        },
        {
          "key": "booking-exhibition",
          "titleZh": "展览参观",
          "hint": "A visitor enquires about an exhibition. The staff member gives opening times, ticket prices, concession conditions, guided-tour schedules, and venue directions."
        },
        {
          "key": "booking-festival",
          "titleZh": "节庆活动",
          "hint": "A resident asks about a local festival. The organiser covers the programme by day, ticket types, venue location, parking arrangements, and a contact name."
        }
      ]
    },
    {
      "id": "accommodation",
      "titleZh": "住宿",
      "scenarios": [
        {
          "key": "accommodation-rental",
          "titleZh": "租房咨询",
          "hint": "A tenant enquires about a property to rent. The agent gives the address, monthly rent, number of bedrooms, deposit conditions, available date, and nearby transport."
        },
        {
          "key": "accommodation-student-hall",
          "titleZh": "学生宿舍",
          "hint": "A new student asks about university halls. The accommodation officer covers room options, weekly cost, catering arrangements, application deadline, and what to bring."
        }
      ]
    },
    {
      "id": "employment",
      "titleZh": "求职",
      "scenarios": [
        {
          "key": "employment-vacancy",
          "titleZh": "职位空缺咨询",
          "hint": "An applicant enquires about advertised vacancies. The employer describes several roles with differing shift patterns, hourly pay, age or licence requirements, and weekend obligations, so the applicant must compare conditions."
        },
        {
          "key": "employment-summer-job",
          "titleZh": "暑期工应聘",
          "hint": "A student applies for summer work. The manager gives start date, weekly hours, rate of pay, training arrangements, uniform requirements, and a supervisor's name."
        }
      ]
    },
    {
      "id": "customer_service",
      "titleZh": "客服/事务",
      "scenarios": [
        {
          "key": "service-refund",
          "titleZh": "退款投诉",
          "hint": "A customer reports a faulty order and requests a refund. The agent takes the order number, confirms the purchase date, explains the returns condition, refund amount, and processing time."
        },
        {
          "key": "service-cleaning",
          "titleZh": "家政保洁",
          "hint": "A householder books a cleaning service. The provider covers service packages, hourly rate, visit frequency, what is excluded, access arrangements, and a start date."
        },
        {
          "key": "service-brochure",
          "titleZh": "宣传册订单",
          "hint": "A customer orders printed brochures. The clerk takes quantity, paper size, delivery method (with an email-or-post choice), unit price, total, and a contact surname to spell."
        }
      ]
    },
    {
      "id": "community",
      "titleZh": "社会参与",
      "scenarios": [
        {
          "key": "community-environment",
          "titleZh": "环保报名",
          "hint": "A volunteer signs up for an environmental project. The coordinator gives meeting point, dates, equipment provided, minimum age, session length, and a registration code."
        },
        {
          "key": "community-event-organising",
          "titleZh": "活动组织",
          "hint": "A member helps organise a community event. The organiser covers the venue, setup time, number of helpers needed, catering budget, and who to report to."
        }
      ]
    },
    {
      "id": "daily_services",
      "titleZh": "生活服务",
      "scenarios": [
        {
          "key": "daily-health-centre",
          "titleZh": "健康中心",
          "hint": "A new patient registers at a health centre. The receptionist takes personal details, confirms the address and phone number, explains appointment hours, and gives the practice location and required documents."
        },
        {
          "key": "daily-driving-lessons",
          "titleZh": "驾驶课",
          "hint": "A learner asks about driving lessons. The instructor covers manual versus automatic, lesson price, package discount, test fee, instructor name to spell, and lesson times."
        }
      ]
    }
  ]
} as const
