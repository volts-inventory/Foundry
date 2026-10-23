Feature: T1

  Scenario: AC-1
    Given an empty list
    When I add "milk"
    Then the list has 1 item

  Scenario: AC-2
    Given an empty list
    When I add "milk"
    Then the list has 2 items

  Scenario: AC-3
    Given an empty list
    When I remove "milk"
    Then the list has 0 items

  Scenario: AC-4
    Given anything at all
    And whatever else
    Then it is fine
